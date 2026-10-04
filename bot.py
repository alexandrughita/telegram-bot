import asyncio
import hmac
import html
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes,
                          MessageHandler, filters)

from db import Store
from moderation import build_fingerprint, duplicate_reason, links_to_approape, violation_action
import posts

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Render sets RENDER_EXTERNAL_URL itself, so WEBHOOK_URL is only needed elsewhere.
WEBHOOK_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
# Shared with the external cron that calls /tick; scheduled posts are off without it.
TICK_SECRET = os.environ.get("TICK_SECRET", "")

# 0 until known: the bot then only answers /chatid, which is how you find them.
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID") or 0)
SUPPORT_CHAT_ID = int(os.environ.get("SUPPORT_CHAT_ID") or 0)

INVITES_REQUIRED = int(os.environ.get("INVITES_REQUIRED", "3"))
DUPLICATE_COOLDOWN_HOURS = float(os.environ.get("DUPLICATE_COOLDOWN_HOURS", "6"))
GIF_WINDOW_SECONDS = int(os.environ.get("GIF_WINDOW_SECONDS", "60"))
GIF_MAX_IN_WINDOW = int(os.environ.get("GIF_MAX_IN_WINDOW", "1"))
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.92"))
VIOLATION_WINDOW_HOURS = float(os.environ.get("VIOLATION_WINDOW_HOURS", "48"))
MUTE_MINUTES = int(os.environ.get("MUTE_MINUTES", "60"))
PORT = int(os.environ.get("PORT", "10000"))

WELCOME_TTL_SECONDS = 180
# Two animated/video sticker messages and two static stickers per member per day.
STICKER_WINDOW_HOURS = 24
STICKERS_PER_DAY = 2
NOTICE_TTL_SECONDS = 60
CACHE_TTL_SECONDS = 300

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("telegram-bot")

store: Store = None  # opened in main()

ADMIN_STATUSES = ("creator", "administrator")
READ_ONLY = ChatPermissions.no_permissions()
# Used only if the group's own default permissions cannot be read.
FALLBACK_POSTING = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True,
)

_admin_cache = {}        # chat_id -> (expires_at, set of admin ids)
_permissions_cache = {}  # chat_id -> (expires_at, ChatPermissions)
_site_handles = None     # (expires_at, set of usernames published on approape.ro profiles)
_support_ack_at = {}     # user_id -> last time we acknowledged a help message
_background = set()      # strong refs to fire-and-forget tasks
# Telegram delivers updates over parallel webhook requests (a whole backlog at once
# when Render wakes up). Each check reads what earlier messages saved, so group
# messages are moderated one at a time or a burst slips through unchecked.
_moderation_lock = asyncio.Lock()
_tick_lock = asyncio.Lock()
_last_cleanup = 0.0


def in_chat(member):
    if member.status in ("creator", "administrator", "member"):
        return True
    return member.status == "restricted" and getattr(member, "is_member", False)


def spawn(coro):
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def delete_later(bot, chat_id, message_id, seconds):
    await asyncio.sleep(seconds)
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def send_temporary(bot, chat_id, text, seconds, **kwargs):
    try:
        sent = await bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
        spawn(delete_later(bot, chat_id, sent.message_id, seconds))
    except Exception as exc:
        log.warning("Could not send notice: %s", exc)


# ------------------------------------------------------------
# Admins and permissions, read live from Telegram
# ------------------------------------------------------------
async def is_admin(bot, chat_id, user_id):
    cached = _admin_cache.get(chat_id)
    if not cached or cached[0] < time.monotonic():
        admins = await bot.get_chat_administrators(chat_id)
        cached = (time.monotonic() + CACHE_TTL_SECONDS, {a.user.id for a in admins})
        _admin_cache[chat_id] = cached
    return user_id in cached[1]


async def posting_permissions(bot, chat_id):
    cached = _permissions_cache.get(chat_id)
    if not cached or cached[0] < time.monotonic():
        try:
            chat = await bot.get_chat(chat_id)
            perms = chat.permissions or FALLBACK_POSTING
        except Exception as exc:
            log.warning("Could not read group permissions: %s", exc)
            perms = FALLBACK_POSTING
        cached = (time.monotonic() + CACHE_TTL_SECONDS, perms)
        _permissions_cache[chat_id] = cached
    return cached[1]


async def restrict(bot, chat_id, user_id):
    try:
        await bot.restrict_chat_member(chat_id, user_id, READ_ONLY, use_independent_chat_permissions=True)
    except Exception as exc:
        log.warning("Could not restrict %s: %s", user_id, exc)


async def allow_posting(bot, chat_id, user_id):
    try:
        await bot.restrict_chat_member(
            chat_id, user_id, await posting_permissions(bot, chat_id),
            use_independent_chat_permissions=True)
    except Exception as exc:
        log.warning("Could not unlock %s: %s", user_id, exc)


async def linked_on_site(user):
    """Her Telegram username is published on an approape.ro profile."""
    global _site_handles
    if not user.username:
        return False
    if not _site_handles or _site_handles[0] < time.monotonic():
        try:
            handles = await posts.fetch_telegram_handles()
        except Exception as exc:
            # Fails closed: she is then held to the invite rule like everyone else.
            log.warning("Could not read Telegram handles from the site: %s", exc)
            handles = set()
        _site_handles = (time.monotonic() + CACHE_TTL_SECONDS, handles)
    return user.username.lower() in _site_handles[1]


async def unlock_for_site_profile(bot, user):
    """Unlocks a member whose Telegram is on her approape.ro profile. True if it did."""
    if not await linked_on_site(user):
        return False
    await store.set_unlocked(GROUP_CHAT_ID, user.id)
    await allow_posting(bot, GROUP_CHAT_ID, user.id)
    await store.log_event(GROUP_CHAT_ID, user.id, "unlock", "Telegram pe profilul approape.ro")
    return True


# ------------------------------------------------------------
# Invite system
# ------------------------------------------------------------
def invite_button(bot):
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔗 Linkul meu de invitație", url=f"https://t.me/{bot.username}?start=invite")]])


async def personal_link(bot, user_id):
    link = await store.get_invite_link(GROUP_CHAT_ID, user_id)
    if link:
        return link
    created = await bot.create_chat_invite_link(GROUP_CHAT_ID, name=f"inv-{user_id}")
    await store.save_invite_link(GROUP_CHAT_ID, user_id, created.invite_link)
    return created.invite_link


async def maybe_unlock(bot, inviter_id, inviter=None):
    member = await store.get_member(GROUP_CHAT_ID, inviter_id)
    if member is None and inviter:
        # Added people by hand before the bot ever saw her: she was there before it.
        member = await store.add_member(GROUP_CHAT_ID, inviter, legacy=True)
    if not member or member["unlocked"]:
        return
    if await store.invite_count(GROUP_CHAT_ID, inviter_id) < INVITES_REQUIRED:
        return
    await store.set_unlocked(GROUP_CHAT_ID, inviter_id)
    await allow_posting(bot, GROUP_CHAT_ID, inviter_id)
    await store.log_event(GROUP_CHAT_ID, inviter_id, "unlock", f"{INVITES_REQUIRED} invitații")
    try:
        await bot.send_message(inviter_id, f"✅ Ai adus {INVITES_REQUIRED} membri — acum poți posta în grup.")
    except Exception:
        pass  # she never opened a private chat with the bot


async def on_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if not cm or cm.chat.id != GROUP_CHAT_ID:
        return
    old, new = cm.old_chat_member, cm.new_chat_member
    user = new.user
    if old.status in ADMIN_STATUSES or new.status in ADMIN_STATUSES:
        _admin_cache.pop(GROUP_CHAT_ID, None)
    if user.is_bot or in_chat(old) or not in_chat(new):
        return  # not a join: a promotion, a restriction, a departure, or a bot

    bot = context.bot
    if new.status in ADMIN_STATUSES:
        await store.add_member(GROUP_CHAT_ID, user, unlocked=True)
        return

    whitelisted = await store.is_whitelisted(GROUP_CHAT_ID, user.id)
    existing = await store.get_member(GROUP_CHAT_ID, user.id)
    if existing:
        # Coming back: restore what she had, and no invite credit for anyone.
        if existing["unlocked"] or whitelisted:
            await allow_posting(bot, GROUP_CHAT_ID, user.id)
        elif not await unlock_for_site_profile(bot, user):
            await restrict(bot, GROUP_CHAT_ID, user.id)
        return

    await store.add_member(GROUP_CHAT_ID, user)
    can_post = whitelisted or await unlock_for_site_profile(bot, user)
    if whitelisted:
        await allow_posting(bot, GROUP_CHAT_ID, user.id)
    elif not can_post:
        await restrict(bot, GROUP_CHAT_ID, user.id)

    # Credited: whoever owns the bot link she joined through, or whoever added her by
    # hand. A link the bot did not create is credited to nobody.
    inviter_id, inviter = None, None
    if cm.invite_link:
        inviter_id = await store.inviter_for_link(GROUP_CHAT_ID, cm.invite_link.invite_link)
    elif cm.from_user and cm.from_user.id != user.id and not cm.from_user.is_bot:
        inviter_id, inviter = cm.from_user.id, cm.from_user
    if inviter_id and inviter_id != user.id:
        if await store.record_invite(GROUP_CHAT_ID, user.id, inviter_id):
            await maybe_unlock(bot, inviter_id, inviter)

    if can_post:
        return  # no welcome asking her for invites she does not need
    await send_temporary(
        bot, GROUP_CHAT_ID,
        f"Bun venit, {user.mention_html()}! Ca să poți posta, adu {INVITES_REQUIRED} membri "
        f"(preferabil foști clienți care te recomandă sau fete care fac web/întâlniri) "
        f"prin linkul tău personal sau pune-ți Telegramul pe profilul tău de pe approape.ro.",
        WELCOME_TTL_SECONDS, reply_markup=invite_button(bot))


async def invite_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Private chat: the user's single personal link and how far along she is."""
    bot, user = context.bot, update.effective_user
    if not GROUP_CHAT_ID:
        await update.effective_message.reply_text("Botul nu este configurat încă.")
        return
    membership = await bot.get_chat_member(GROUP_CHAT_ID, user.id)
    if not in_chat(membership):
        await update.effective_message.reply_text("Intră întâi în grup, apoi revino aici.")
        return
    if membership.status in ADMIN_STATUSES:
        await update.effective_message.reply_text("Ești admin în grup — poți posta oricând.")
        return
    if await store.is_whitelisted(GROUP_CHAT_ID, user.id):
        await update.effective_message.reply_text("Ești pe lista albă a grupului — poți posta oricând.")
        return

    member = await store.get_member(GROUP_CHAT_ID, user.id)
    if member is None:
        # In the group, but the bot never saw her join: she was there before it.
        # She still needs her invites, like everyone else.
        member = await store.add_member(GROUP_CHAT_ID, user, legacy=True)
    if not member["unlocked"] and await unlock_for_site_profile(bot, user):
        await update.effective_message.reply_text(
            "✅ Telegramul tău e pe profilul tău de pe approape.ro — acum poți posta în grup.")
        return

    try:
        link = await personal_link(bot, user.id)
    except Exception as exc:
        log.exception("create_chat_invite_link failed: %s", exc)
        await update.effective_message.reply_text("Nu pot crea linkul acum. Încearcă din nou mai târziu.")
        return
    count = await store.invite_count(GROUP_CHAT_ID, user.id)
    if member["unlocked"]:
        state = "✅ Poți posta în grup."
    else:
        state = (f"Mai ai nevoie de {INVITES_REQUIRED - count} (preferabil foști clienți care te recomandă "
                 f"sau fete care fac web/întâlniri) ca să poți posta. Sau pune-ți "
                 f"Telegramul (@{user.username or 'numele_tău'}) pe profilul tău de pe approape.ro "
                 f"și scrie-mi din nou /start.")
    await update.effective_message.reply_text(
        f"🔗 Linkul tău:\n{link}\n\n👥 Invitații: {min(count, INVITES_REQUIRED)}/{INVITES_REQUIRED}\n{state}",
        disable_web_page_preview=True)


# Help links on the site open the bot as t.me/<bot>?start=<topic>. Each topic tells
# the person what to send, and the support chat which problem they came with.
HELP_TOPICS = {
    "ajutor": (
        "Scrie-ne aici cu ce te putem ajuta. Echipa approape.ro îți răspunde în această conversație.",
        None),
    "cont": (
        "Ne pare rău că nu ți-ai putut face cont. Ca să te ajutăm, scrie-ne aici:\n"
        "1. numărul de telefon cu care ai încercat;\n"
        "2. ce eroare ai văzut (o captură de ecran e cel mai bine);\n"
        "3. dacă ești escortă, creatoare sau client.\n\n"
        "Între timp poți intra pe approape.ro cu Google — merge și când SMS-ul nu vine.\n"
        "Îți răspundem aici.",
        "nu își poate face cont"),
    "revendicare": (
        "Vrei să-ți revendici profilul de pe approape.ro și SMS-ul nu ajunge. Scrie-ne aici:\n"
        "1. linkul profilului tău de pe approape.ro;\n"
        "2. numărul de telefon de pe profil;\n"
        "3. o captură de ecran cu eroarea, dacă ai.\n\n"
        "Verificăm că profilul e al tău și îți răspundem aici.",
        "vrea să-și revendice profilul, SMS-ul nu ajunge"),
}


# The menu /start shows. Each button answers on the spot; only what it cannot answer
# reaches the support chat, by the person writing to the bot.
MENU = [
    ("sms", "📱 Nu primesc SMS-ul"),
    ("revendicare", "🔑 Revendicare profil"),
    ("grup", "💬 Cum pot posta în grup"),
    ("telegram", "✈️ Telegram pe profilul meu"),
    ("om", "🙋 Vorbește cu un om"),
]
MENU_ANSWERS = {
    "sms": "Când SMS-ul nu vine, intră pe approape.ro cu Google: în fereastra de autentificare "
           "alege «Continuă cu Google». Durează câteva secunde și îți poți face profilul de acolo.\n\n"
           "Dacă nici așa nu merge, apasă «Vorbește cu un om».",
    "telegram": "Pune-ți Telegramul pe profil din approape.ro: «Contul meu» → cardul «Telegram» → "
                "scrie @numele_tău.\n\nCu Telegramul pe profil poți posta în grup fără invitații: "
                "după ce l-ai salvat, scrie-mi /start.",
    "om": "Scrie-ne aici mesajul tău. Îl primește echipa approape.ro și îți răspundem în această conversație.",
}


def menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"menu:{key}")]
                                 for key, label in MENU])


def answer_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🙋 Vorbește cu un om", callback_data="menu:om")],
        [InlineKeyboardButton("« Înapoi la meniu", callback_data="menu:meniu")],
    ])


async def show_menu(message):
    await message.reply_text("Salut! Cu ce te putem ajuta?", reply_markup=menu_keyboard())


async def help_topic(update, context, key):
    """A help topic from the site's links: what to send, and an alert to support."""
    reply, support_note = HELP_TOPICS[key]
    await update.effective_message.reply_text(reply)
    if support_note and SUPPORT_CHAT_ID:
        user = update.effective_user
        note = await context.bot.send_message(
            SUPPORT_CHAT_ID,
            f"🆘 {user.full_name}" + (f" (@{user.username})" if user.username else "") + f" · id {user.id}\n"
            f"Vine de pe site: {support_note}. Răspunde cu reply aici.")
        await store.save_support_thread(note.message_id, user.id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payload = context.args[0] if context.args else ""
    if payload == "invite":  # the button in the group's welcome and lock notices
        await invite_status(update, context)
    elif payload in HELP_TOPICS:
        await help_topic(update, context, payload)
    else:
        await show_menu(update.effective_message)


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.removeprefix("menu:")
    if key == "grup":
        await invite_status(update, context)
    elif key == "revendicare":
        # Claiming needs an SMS to the profile's number; when it does not arrive,
        # only a person can check the profile is hers.
        await help_topic(update, context, "revendicare")
    elif key in MENU_ANSWERS:
        await query.message.reply_text(
            MENU_ANSWERS[key], reply_markup=None if key == "om" else answer_keyboard())
    else:
        await show_menu(query.message)


async def cmd_group_redirect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/invite or /status typed in the group: the answer is personal, so it goes private."""
    await send_temporary(
        context.bot, update.effective_chat.id, "Îți arăt linkul și progresul în privat:",
        NOTICE_TTL_SECONDS, reply_markup=invite_button(context.bot))


# ------------------------------------------------------------
# Moderation
# ------------------------------------------------------------
def fingerprint_of(message):
    text = message.text or message.caption or ""
    entities = {**message.parse_entities(), **message.parse_caption_entities()}
    urls, phones = [], []
    for entity, value in entities.items():
        if entity.type == MessageEntity.TEXT_LINK:
            urls.append(entity.url)
        elif entity.type == MessageEntity.URL:
            urls.append(value)
        elif entity.type == MessageEntity.PHONE_NUMBER:
            phones.append(value)
    media = []
    if message.photo:
        media.append(message.photo[-1].file_unique_id)
    for item in (message.video, message.animation, message.document, message.video_note):
        if item:
            media.append(item.file_unique_id)
    return build_fingerprint(text, urls, phones, media)


async def punish(bot, message, reason):
    chat_id, user = message.chat_id, message.from_user
    try:
        await bot.delete_message(chat_id, message.message_id)
    except Exception as exc:
        log.warning("Delete failed: %s", exc)

    count = await store.add_violation(chat_id, user.id, reason, VIOLATION_WINDOW_HOURS)
    action = violation_action(count)
    log.info("violation user=%s reason=%s count=%s action=%s", user.id, reason, count, action)
    await store.log_event(chat_id, user.id, action, reason, message.message_id)
    if action == "warn":
        await send_temporary(
            bot, chat_id,
            f"⚠️ {user.mention_html()}, mesaj șters ({reason}). Avertisment {count - 1}/2 — "
            f"la următoarea abatere primești mute {MUTE_MINUTES} de minute.",
            NOTICE_TTL_SECONDS)
    elif action == "mute":
        until = datetime.now(timezone.utc) + timedelta(minutes=MUTE_MINUTES)
        try:
            await bot.restrict_chat_member(
                chat_id, user.id, READ_ONLY, until_date=until, use_independent_chat_permissions=True)
        except Exception as exc:
            log.warning("Mute failed: %s", exc)
        await send_temporary(
            bot, chat_id,
            f"🔇 {user.mention_html()} are mute {MUTE_MINUTES} de minute ({reason}).",
            NOTICE_TTL_SECONDS)


async def maybe_cleanup():
    global _last_cleanup
    if time.monotonic() - _last_cleanup < 3600:
        return
    _last_cleanup = time.monotonic()
    keep_hours = max(DUPLICATE_COOLDOWN_HOURS, GIF_WINDOW_SECONDS / 3600, STICKER_WINDOW_HOURS)
    await store.cleanup(keep_hours)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or message.chat_id != GROUP_CHAT_ID or message.is_automatic_forward:
        return  # automatic forwards are posts from the group's linked channel
    bot = context.bot
    if message.from_user and not message.from_user.is_bot:
        # Scheduled posts wait for this, so the bot never talks into an empty room.
        await store.set_time("last_human_at", datetime.now(timezone.utc))

    if message.sender_chat:
        if message.sender_chat.id == GROUP_CHAT_ID:
            return  # an admin posting anonymously
        # Posting "as a channel" would sidestep every per-user rule.
        try:
            await bot.delete_message(message.chat_id, message.message_id)
        except Exception:
            pass
        await store.log_event(GROUP_CHAT_ID, message.sender_chat.id, "delete", "postare ca un canal",
                              message.message_id)
        return

    user = message.from_user
    if not user or user.is_bot or await is_admin(bot, GROUP_CHAT_ID, user.id):
        return
    if await store.is_whitelisted(GROUP_CHAT_ID, user.id):
        return  # exempted by an admin with /whitelist

    fp = fingerprint_of(message)
    if links_to_approape(fp.urls):
        return  # a link to approape.ro may be posted any time, by anyone in the group

    async with _moderation_lock:
        member = await store.get_member(GROUP_CHAT_ID, user.id)
        if member is None:
            # The bot never saw her join, so she was in the group before it. She still
            # needs her invites; Telegram has not restricted her yet, so that happens below.
            member = await store.add_member(GROUP_CHAT_ID, user, legacy=True)
        if not member["unlocked"] and not await unlock_for_site_profile(bot, user):
            try:
                await bot.delete_message(message.chat_id, message.message_id)
            except Exception:
                pass
            await store.log_event(GROUP_CHAT_ID, user.id, "delete", "fără drept de postare", message.message_id)
            await restrict(bot, GROUP_CHAT_ID, user.id)
            await send_temporary(
                bot, GROUP_CHAT_ID,
                f"{user.mention_html()}, ca să poți posta, adu {INVITES_REQUIRED} membri "
                f"(preferabil foști clienți care te recomandă sau fete care fac web/întâlniri) "
                f"prin linkul tău personal sau pune-ți Telegramul pe profilul tău de pe approape.ro.",
                NOTICE_TTL_SECONDS, reply_markup=invite_button(bot))
            return

        is_gif = message.animation is not None
        # Animated/video stickers and static ones are limited per day, each counted separately.
        sticker = message.sticker
        is_sticker = bool(sticker and (sticker.is_animated or sticker.is_video))
        is_static_sticker = bool(sticker) and not is_sticker
        if sticker and await store.recent_sticker_count(
                GROUP_CHAT_ID, user.id, STICKER_WINDOW_HOURS, static=is_static_sticker) >= STICKERS_PER_DAY:
            # Deleted and explained, but not a violation: no warning, no mute.
            try:
                await bot.delete_message(message.chat_id, message.message_id)
            except Exception:
                pass
            await store.log_event(GROUP_CHAT_ID, user.id, "delete", "stickere: limita zilnică",
                                  message.message_id)
            await send_temporary(
                bot, GROUP_CHAT_ID,
                f"{user.mention_html()}, poți trimite cel mult {STICKERS_PER_DAY} reclame pe zi. "
                f"Mai bine scrie-ne ceva: o întrebare, o recomandare sau o experiență de povestit. "
                f"Mesajele adevărate țin grupul viu și aduc răspunsuri 🙂",
                NOTICE_TTL_SECONDS)
            return
        if is_gif and await store.recent_gif_count(GROUP_CHAT_ID, user.id, GIF_WINDOW_SECONDS) >= GIF_MAX_IN_WINDOW:
            await punish(bot, message, "prea multe GIF-uri la rând")
            return

        earlier = await store.recent_fingerprints(GROUP_CHAT_ID, user.id, DUPLICATE_COOLDOWN_HOURS)
        reason = duplicate_reason(fp, earlier, SIMILARITY_THRESHOLD)
        if reason:
            await punish(bot, message, f"reclamă repetată: {reason}")
            return

        await store.save_message(GROUP_CHAT_ID, user.id, message.message_id, fp, is_gif, is_sticker,
                                 is_static_sticker)
        await maybe_cleanup()


# ------------------------------------------------------------
# Help desk: private chat <-> support chat
# ------------------------------------------------------------
async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message, user, bot = update.effective_message, update.effective_user, context.bot
    if not SUPPORT_CHAT_ID:
        await message.reply_text("Momentan nu putem primi mesaje aici. Scrie-ne pe approape.ro.")
        return
    header = await bot.send_message(
        SUPPORT_CHAT_ID,
        f"📩 {user.full_name}" + (f" (@{user.username})" if user.username else "") + f" · id {user.id}\n"
        "Răspunde cu reply la mesajul de mai jos.")
    copy = await bot.copy_message(SUPPORT_CHAT_ID, message.chat_id, message.message_id)
    await store.save_support_thread(header.message_id, user.id)
    await store.save_support_thread(copy.message_id, user.id)

    last = _support_ack_at.get(user.id, 0)
    if time.monotonic() - last > 1800:
        _support_ack_at[user.id] = time.monotonic()
        await message.reply_text("Am primit mesajul. Îți răspundem aici cât de repede putem.")


async def on_support_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message.reply_to_message:
        return
    user_id = await store.support_user_for(message.reply_to_message.message_id)
    if not user_id:
        return
    try:
        await context.bot.copy_message(user_id, message.chat_id, message.message_id)
    except Exception as exc:
        await message.reply_text(f"Nu am putut trimite răspunsul: {exc}")


# ------------------------------------------------------------
# Scheduled posts (driven by /tick)
# ------------------------------------------------------------
async def post_profile(bot, kind, profile):
    caption = posts.profile_caption(kind, profile)
    try:
        # Downloaded rather than passed as a URL: the site's photos are WebP, which
        # Telegram handles reliably as an upload.
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(profile.photo)
            resp.raise_for_status()
        await bot.send_photo(GROUP_CHAT_ID, photo=resp.content, caption=caption, parse_mode="HTML")
    except Exception as exc:
        log.warning("Photo post failed (%s); posting the link instead", exc)
        await bot.send_message(GROUP_CHAT_ID, caption, parse_mode="HTML")


async def post_question(bot, index):
    question = posts.QUESTIONS[index]
    if "poll" in question:
        await bot.send_poll(GROUP_CHAT_ID, question["poll"], question["options"], is_anonymous=True)
    else:
        await bot.send_message(GROUP_CHAT_ID, question["text"])


async def run_tick(bot):
    """Post if it is time; otherwise do nothing. Called by the external cron."""
    async with _tick_lock:
        now = datetime.now(timezone.utc)
        next_at = await store.get_time("next_post_at")
        if next_at is None:
            await store.set_time("next_post_at", posts.next_post_time(now))
            return
        last = await store.last_post(GROUP_CHAT_ID)
        last_human_at = await store.get_time("last_human_at")
        if not posts.due(now, next_at, last and last["posted_at"], last_human_at):
            return

        kind = profile = None
        if last is None or last["kind"] == "question":
            try:
                recent = await store.recent_post_refs(GROUP_CHAT_ID, posts.PROFILE_REPEAT_DAYS)
                kind, profile = posts.pick_profile(*await posts.fetch_site_profiles(now), recent, now)
            except Exception as exc:
                log.warning("Could not read profiles from the site: %s", exc)
        if profile:
            await post_profile(bot, kind, profile)
            await store.record_post(GROUP_CHAT_ID, kind, profile.path, now)
        else:
            used = await store.recent_post_refs(GROUP_CHAT_ID, posts.QUESTION_REPEAT_DAYS, question=True)
            priority_used = await store.recent_post_refs(GROUP_CHAT_ID, posts.PRIORITY_REPEAT_DAYS, question=True)
            index = posts.pick_question(used, priority_used)
            await post_question(bot, index)
            await store.record_post(GROUP_CHAT_ID, "question", str(index), now)
        await store.set_time("next_post_at", posts.next_post_time(now))


# ------------------------------------------------------------
# Admin commands
# ------------------------------------------------------------
async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    log.warning("GROUP CHAT ID DETECTED: %s", chat.id)
    if chat.type != "private" and not await is_admin(context.bot, chat.id, update.effective_user.id):
        return
    await update.effective_message.reply_text(f"chat id: {chat.id}")


def target_user(message, args):
    """(user_id, display name) from the replied-to message, or from an id argument."""
    replied = message.reply_to_message
    if replied and replied.from_user and not replied.from_user.is_bot and not replied.sender_chat:
        return replied.from_user.id, replied.from_user.mention_html()
    if args and args[0].lstrip("-").isdigit():
        return int(args[0]), f"id {args[0]}"
    return None, None


async def cmd_whitelist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/whitelist or /unwhitelist, by an admin, as a reply to the person or with her id."""
    message, bot = update.effective_message, context.bot
    if not await is_admin(bot, GROUP_CHAT_ID, update.effective_user.id):
        return
    user_id, name = target_user(message, context.args)
    adding = message.text.split()[0].split("@")[0].lower() == "/whitelist"
    if user_id is None:
        await send_temporary(
            bot, GROUP_CHAT_ID,
            f"Dă reply la un mesaj al persoanei cu {'/whitelist' if adding else '/unwhitelist'}, "
            f"sau scrie id-ul ei după comandă.",
            NOTICE_TTL_SECONDS)
        return
    if adding:
        await store.add_whitelist(GROUP_CHAT_ID, user_id, update.effective_user.id)
        # Lifts a lock or a mute she may already be under.
        await allow_posting(bot, GROUP_CHAT_ID, user_id)
        await store.log_event(GROUP_CHAT_ID, user_id, "whitelist", f"de {update.effective_user.id}")
        text = f"✅ {name} e pe lista albă: nicio regulă a botului nu i se mai aplică."
    elif await store.remove_whitelist(GROUP_CHAT_ID, user_id):
        await store.log_event(GROUP_CHAT_ID, user_id, "unwhitelist", f"de {update.effective_user.id}")
        text = f"{name} nu mai e pe lista albă: regulele obișnuite i se aplică din nou."
    else:
        text = f"{name} nu era pe lista albă."
    await send_temporary(bot, GROUP_CHAT_ID, text, NOTICE_TTL_SECONDS)


async def admin_reply(update, bot, text):
    """In the group the answer vanishes after a minute; in private it stays."""
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(text, parse_mode="HTML")
    else:
        await send_temporary(bot, GROUP_CHAT_ID, text, NOTICE_TTL_SECONDS)


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/info, by an admin, as a reply to the person or with her id: why she can or cannot post."""
    message, bot = update.effective_message, context.bot
    if not await is_admin(bot, GROUP_CHAT_ID, update.effective_user.id):
        return
    user_id, name = target_user(message, context.args)
    if user_id is None:
        await admin_reply(update, bot, "Dă reply la un mesaj al persoanei cu /info, sau scrie id-ul ei după comandă.")
        return
    try:
        membership = await bot.get_chat_member(GROUP_CHAT_ID, user_id)
        tg_user, status = membership.user, membership.status
    except Exception:
        tg_user, status = None, "necunoscut"
    member = await store.get_member(GROUP_CHAT_ID, user_id)
    lines = [f"ℹ️ {name} · id {user_id}" + (f" · @{tg_user.username}" if tg_user and tg_user.username else "")]
    if await is_admin(bot, GROUP_CHAT_ID, user_id):
        lines.append("Admin: poate posta oricând.")
    elif await store.is_whitelisted(GROUP_CHAT_ID, user_id):
        lines.append("Pe lista albă: poate posta, nicio regulă nu i se aplică.")
    elif member and member["unlocked"]:
        lines.append("Poate posta.")
    else:
        lines.append("Nu poate posta încă.")
    lines.append(f"În grup: {status}" + (" · din grup înainte de bot" if member and member["legacy"] else "")
                 + ("" if member else " · botul n-a văzut-o încă"))
    lines.append(f"Invitații: {await store.invite_count(GROUP_CHAT_ID, user_id)}/{INVITES_REQUIRED}")
    on_site = bool(tg_user) and await linked_on_site(tg_user)
    lines.append(f"Telegram pe un profil approape.ro: {'da' if on_site else 'nu'}")
    lines.append(f"Abateri în ultimele {VIOLATION_WINDOW_HOURS:g}h: "
                 f"{await store.violation_count(GROUP_CHAT_ID, user_id, VIOLATION_WINDOW_HOURS)}")
    events = await store.recent_events(GROUP_CHAT_ID, user_id, 5)
    if events:
        lines.append("Ultimele acțiuni:")
        for e in events:
            when = e["created_at"].astimezone(posts.LOCAL_TZ).strftime("%d.%m %H:%M")
            lines.append(f"• {when} {e['event_type']}" + (f" — {html.escape(e['reason'])}" if e["reason"] else ""))
    await admin_reply(update, bot, "\n".join(lines))


async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/unlock, by an admin: she may post without her invites. Unlike /whitelist,
    every other rule still applies to her."""
    message, bot = update.effective_message, context.bot
    if not await is_admin(bot, GROUP_CHAT_ID, update.effective_user.id):
        return
    user_id, name = target_user(message, context.args)
    if user_id is None:
        await admin_reply(update, bot, "Dă reply la un mesaj al persoanei cu /unlock, sau scrie id-ul ei după comandă.")
        return
    if not await store.get_member(GROUP_CHAT_ID, user_id):
        try:
            tg_user = (await bot.get_chat_member(GROUP_CHAT_ID, user_id)).user
        except Exception:
            await admin_reply(update, bot, f"Nu găsesc {name} în grup.")
            return
        await store.add_member(GROUP_CHAT_ID, tg_user, legacy=True)
    await store.set_unlocked(GROUP_CHAT_ID, user_id)
    await allow_posting(bot, GROUP_CHAT_ID, user_id)  # also lifts a mute
    await store.log_event(GROUP_CHAT_ID, user_id, "unlock", f"manual, de {update.effective_user.id}")
    await admin_reply(update, bot, f"✅ {name} poate posta. Celelalte reguli i se aplică în continuare.")


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(context.bot, GROUP_CHAT_ID, update.effective_user.id):
        return
    s = await store.stats(GROUP_CHAT_ID)
    await update.effective_message.reply_text(
        f"📊 Membri urmăriți: {s['members']}\nPot posta: {s['unlocked']}\n"
        f"Invitații: {s['invites']}\nAbateri (7 zile): {s['violations_7d']}")


def build_application():
    application = Application.builder().token(BOT_TOKEN).updater(None).build()
    private = filters.ChatType.PRIVATE
    group = filters.Chat(GROUP_CHAT_ID) if GROUP_CHAT_ID else filters.ChatType.GROUPS

    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler("stats", cmd_stats, filters=group))
    application.add_handler(CommandHandler(["whitelist", "unwhitelist"], cmd_whitelist, filters=group))
    application.add_handler(CommandHandler("info", cmd_info, filters=group | private))
    application.add_handler(CommandHandler("unlock", cmd_unlock, filters=group | private))
    application.add_handler(CommandHandler("start", cmd_start, filters=private))
    application.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    application.add_handler(CommandHandler(["invite", "status"], invite_status, filters=private))
    application.add_handler(CommandHandler(["invite", "status"], cmd_group_redirect, filters=group))

    if SUPPORT_CHAT_ID:
        support = filters.Chat(SUPPORT_CHAT_ID)
        application.add_handler(MessageHandler(support & ~filters.COMMAND, on_support_reply))

    application.add_handler(MessageHandler(private & ~filters.COMMAND, on_private_message))
    application.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    # Separate handler group so every group message is moderated, commands included:
    # otherwise "/anything <ad>" would slip past the filters.
    application.add_handler(MessageHandler(group & ~filters.StatusUpdate.ALL, on_group_message), group=1)
    return application


# ------------------------------------------------------------
# Web server (Render) + webhook
# ------------------------------------------------------------
app = FastAPI()
tg_app = None


@app.get("/")
async def health():
    # Render sets RENDER_GIT_COMMIT: which commit is live, without opening the dashboard.
    return {"ok": True, "service": "telegram-group-bot", "commit": os.environ.get("RENDER_GIT_COMMIT", "")[:7]}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(received, WEBHOOK_SECRET):
        return Response(status_code=403)
    update = Update.de_json(await request.json(), tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


@app.api_route("/tick", methods=["GET", "POST"])
async def tick(request: Request):
    """Called every few minutes by an external cron: keeps Render awake and drives
    the scheduled posts."""
    received = request.headers.get("X-Tick-Secret", "")
    if not TICK_SECRET or not GROUP_CHAT_ID or not hmac.compare_digest(received, TICK_SECRET):
        return Response(status_code=403)
    try:
        await run_tick(tg_app.bot)
    except Exception:
        log.exception("Scheduled post failed")
    return {"ok": True}


async def main():
    global store, tg_app
    if not DATABASE_URL or len(WEBHOOK_SECRET) < 16:
        raise SystemExit("DATABASE_URL and WEBHOOK_SECRET (min. 16 caractere) sunt obligatorii.")
    store = Store(DATABASE_URL)
    await store.open()
    tg_app = build_application()
    await tg_app.initialize()
    await tg_app.start()

    if WEBHOOK_URL:
        # Set on every start and never deleted on shutdown: Render's free plan
        # stops the service when idle, and deleting the webhook then would mean
        # Telegram stops sending updates and nothing ever wakes it up again.
        await tg_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/telegram/webhook", secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "chat_member", "callback_query"])
        log.info("Webhook set")
    else:
        log.warning("WEBHOOK_URL is not set; no updates will arrive.")
    if not GROUP_CHAT_ID:
        log.warning("GROUP_CHAT_ID is not set; send /chatid in the group to find it.")

    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info"))
    try:
        await server.serve()
    finally:
        await tg_app.stop()
        await tg_app.shutdown()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
