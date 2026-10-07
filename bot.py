import asyncio
import hmac
import html
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.error import Forbidden
from telegram.ext import (
    Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

from db import Store
from moderation import (
    MIN_TEXT_LENGTH_EXACT, build_fingerprint, duplicate_reason, has_external_link, links_to_approape,
    same_ad_text, violation_action,
)
import posts

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "")
WEBHOOK_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
TICK_SECRET = os.environ.get("TICK_SECRET", "")
GROUP_CHAT_ID = int(os.environ.get("GROUP_CHAT_ID") or 0)
SUPPORT_CHAT_ID = int(os.environ.get("SUPPORT_CHAT_ID") or 0)
GROUP_INVITE_URL = os.environ.get("GROUP_INVITE_URL", "").strip()

DUPLICATE_COOLDOWN_HOURS = float(os.environ.get("DUPLICATE_COOLDOWN_HOURS", "6"))
VIOLATION_WINDOW_HOURS = float(os.environ.get("VIOLATION_WINDOW_HOURS", "48"))
MUTE_MINUTES = int(os.environ.get("MUTE_MINUTES", "60"))
PORT = int(os.environ.get("PORT", "10000"))

WELCOME_TTL_SECONDS = 180
NOTICE_TTL_SECONDS = 60
CACHE_TTL_SECONDS = 300
AD_LIMIT = 1
VERIFIED_AD_LIMIT = 3  # she sent the admin a short verification video
AD_WINDOW_HOURS = 24
AD_REMINDER_EVERY_DAYS = 7  # at most one reminder a week
# Static and animated/video stickers are counted separately, 2 of each per day.
STICKERS_PER_DAY = 2
STICKER_WINDOW_HOURS = 24
# Ends every note that deletes an ad or a sticker over the limit.
REAL_MESSAGES_NOTE = ("Mai bine scrie-ne ceva: o întrebare, o recomandare sau o experiență de povestit. "
                      "Mesajele adevărate țin grupul viu și aduc răspunsuri 🙂")

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("telegram-bot")

store = None
ADMIN_STATUSES = ("creator", "administrator")
READ_ONLY = ChatPermissions.no_permissions()
FALLBACK_POSTING = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True,
)
_admin_cache = {}
_permissions_cache = {}
_support_ack_at = {}
_background = set()
_moderation_lock = asyncio.Lock()
_tick_lock = asyncio.Lock()
_last_cleanup = 0.0
_delete_refusal_reported = False
LOCAL_TZ = ZoneInfo("Europe/Bucharest")


def in_chat(member):
    return member.status in ("creator", "administrator", "member") or (
        member.status == "restricted" and getattr(member, "is_member", False)
    )


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


async def delete_member_message(bot, message, user_id, reason):
    """Deletes a member's message; False when Telegram refused. A refusal is logged as
    `delete_failed` with Telegram's reason instead of passing for a deletion, and the first
    one since start is told to support: it usually means the bot lost "Delete messages"."""
    global _delete_refusal_reported
    try:
        await bot.delete_message(message.chat_id, message.message_id)
        return True
    except Exception as exc:
        log.warning("Message %s not deleted: %s", message.message_id, exc)
        await store.log_event(GROUP_CHAT_ID, user_id, "delete_failed", f"Telegram: {exc} — {reason}",
                              message.message_id)
        if SUPPORT_CHAT_ID and not _delete_refusal_reported:
            _delete_refusal_reported = True
            try:
                await bot.send_message(
                    SUPPORT_CHAT_ID,
                    f"⚠️ Telegram nu mă lasă să șterg mesaje din grup ({exc}). "
                    "Verifică dacă botul e admin cu dreptul „Delete messages”.")
            except Exception as alert_exc:
                log.warning("Delete refusal not reported to support: %s", alert_exc)
        return False


async def send_temporary(bot, chat_id, text, seconds, **kwargs):
    try:
        sent = await bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
        spawn(delete_later(bot, chat_id, sent.message_id, seconds))
    except Exception as exc:
        log.warning("Could not send notice: %s", exc)


def ads_word(n):
    return "reclamă" if n == 1 else "reclame"


def verification_offer(group_title):
    """How an unverified member earns VERIFIED_AD_LIMIT: the admin then runs /verifica."""
    group = html.escape(group_title or "grupului")
    return (f"🎥 Fetele verificate pot posta {VERIFIED_AD_LIMIT} reclame pe zi. Trimite un video scurt "
            f"în care spui numele grupului (<b>{group}</b>) și username-ul tău, lui @approape_ro în privat "
            f"sau aici în grup cu @admin. Verificarea îți crește și încrederea clienților.")


async def ad_limit(user_id):
    return VERIFIED_AD_LIMIT if await store.is_verified(GROUP_CHAT_ID, user_id) else AD_LIMIT


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


async def allow_posting(bot, chat_id, user_id):
    try:
        await bot.restrict_chat_member(
            chat_id, user_id, await posting_permissions(bot, chat_id),
            use_independent_chat_permissions=True,
        )
    except Exception as exc:
        log.warning("Could not unlock %s: %s", user_id, exc)


def invite_button(bot):
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔗 Linkul meu de invitație", url=f"https://t.me/{bot.username}?start=invite"
    )]])


async def personal_link(bot, user_id):
    link = await store.get_invite_link(GROUP_CHAT_ID, user_id)
    if link:
        return link
    created = await bot.create_chat_invite_link(GROUP_CHAT_ID, name=f"inv-{user_id}")
    await store.save_invite_link(GROUP_CHAT_ID, user_id, created.invite_link)
    return created.invite_link


async def lift_invite_locks(bot):
    """Members the old 3-invite rule left read-only are still restricted in Telegram itself,
    so dropping the rule in code does not let them post. Idempotent: runs on every start
    and only touches members still stored as locked."""
    try:
        for row in await store.locked_members(GROUP_CHAT_ID):
            await allow_posting(bot, GROUP_CHAT_ID, row["user_id"])
            await store.set_unlocked(GROUP_CHAT_ID, row["user_id"])
            await store.log_event(GROUP_CHAT_ID, row["user_id"], "unlock", "regula de 3 invitații scoasă")
            await asyncio.sleep(0.1)  # stay well under Telegram's rate limit
    except Exception:
        log.exception("Could not lift the old invite locks")


async def on_chat_member(update, context):
    cm = update.chat_member
    if not cm or cm.chat.id != GROUP_CHAT_ID:
        return

    old, new, user = cm.old_chat_member, cm.new_chat_member, cm.new_chat_member.user

    if old.status in ADMIN_STATUSES or new.status in ADMIN_STATUSES:
        _admin_cache.pop(GROUP_CHAT_ID, None)

    if user.is_bot or in_chat(old) or not in_chat(new):
        return

    # Everyone can post as soon as she joins. Invites are still credited, only as a count,
    # and only for a first join: coming back earns nobody anything.
    returning = await store.get_member(GROUP_CHAT_ID, user.id) is not None
    await store.add_member(GROUP_CHAT_ID, user, unlocked=True)
    if returning:
        return
    inviter_id = None
    if cm.invite_link:
        inviter_id = await store.inviter_for_link(GROUP_CHAT_ID, cm.invite_link.invite_link)
    elif cm.from_user and cm.from_user.id != user.id and not cm.from_user.is_bot:
        inviter_id = cm.from_user.id  # added by hand
    if inviter_id and inviter_id != user.id:
        await store.record_invite(GROUP_CHAT_ID, user.id, inviter_id)


async def invite_status(update, context):
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
    try:
        link = await personal_link(bot, user.id)
    except Exception:
        await update.effective_message.reply_text("Nu pot crea linkul acum. Încearcă din nou mai târziu.")
        return
    count = await store.invite_count(GROUP_CHAT_ID, user.id)
    limit = await ad_limit(user.id)
    await update.effective_message.reply_text(
        f"✅ Poți posta în grup.\n\n"
        f"Reguli: cel mult {limit} {ads_word(limit)} în 24 de ore (linkuri externe sau același text repetat), "
        f"{STICKERS_PER_DAY} stickere pe zi și fără aceeași poză, link sau număr repostat.\n\n"
        f"Vrei să aduci pe cineva? 🔗 Linkul tău:\n{link}\n👥 Ai adus: {count}",
        disable_web_page_preview=True,
    )


HELP_TOPICS = {
    "ajutor": ("Scrie-ne aici cu ce te putem ajuta. Echipa approape.ro îți răspunde în această conversație.", None),
    "cont": (
        "Ne pare rău că nu ți-ai putut face cont. Ca să te ajutăm, scrie-ne aici:\n"
        "1. numărul de telefon cu care ai încercat;\n2. ce eroare ai văzut;\n3. dacă ești escortă, creatoare sau client.\n\n"
        "Între timp poți intra pe approape.ro cu Google — merge și când SMS-ul nu vine.",
        "nu își poate face cont",
    ),
    "revendicare": (
        "Vrei să-ți revendici profilul de pe approape.ro și SMS-ul nu ajunge. Scrie-ne aici:\n"
        "1. linkul profilului;\n2. numărul de telefon de pe profil;\n3. o captură de ecran cu eroarea.",
        "vrea să-și revendice profilul, SMS-ul nu ajunge",
    ),
}
MENU = [
    ("sms", "📱 Nu primesc SMS-ul"), ("revendicare", "🔑 Revendicare profil"),
    ("grup", "💬 Cum pot posta în grup"), ("telegram", "✈️ Telegram pe profilul meu"),
    ("om", "🙋 Vorbește cu un om"),
]
MENU_ANSWERS = {
    "sms": "Când SMS-ul nu vine, intră pe approape.ro cu Google: în fereastra de autentificare alege «Continuă cu Google».",
    "telegram": "Pune-ți Telegramul pe profil din approape.ro: «Contul meu» → «Telegram» → scrie @numele_tău.",
    "om": "Scrie-ne aici mesajul tău. Îl primește echipa approape.ro și îți răspundem în această conversație.",
}


def menu_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=f"menu:{key}")] for key, label in MENU])


def answer_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🙋 Vorbește cu un om", callback_data="menu:om")],
        [InlineKeyboardButton("« Înapoi la meniu", callback_data="menu:meniu")],
    ])


async def show_menu(message):
    await message.reply_text("Salut! Cu ce te putem ajuta?", reply_markup=menu_keyboard())


async def help_topic(update, context, key):
    reply, support_note = HELP_TOPICS[key]
    await update.effective_message.reply_text(reply)
    if support_note and SUPPORT_CHAT_ID:
        user = update.effective_user
        note = await context.bot.send_message(
            SUPPORT_CHAT_ID,
            f"🆘 {user.full_name}" + (f" (@{user.username})" if user.username else "") + f" · id {user.id}\n"
            f"Vine de pe site: {support_note}. Răspunde cu reply aici.",
        )
        await store.save_support_thread(note.message_id, user.id)


async def cmd_start(update, context):
    payload = context.args[0] if context.args else ""
    if payload == "invite":
        await invite_status(update, context)
    elif payload in HELP_TOPICS:
        await help_topic(update, context, payload)
    else:
        await show_menu(update.effective_message)


async def on_menu(update, context):
    query = update.callback_query
    await query.answer()
    key = query.data.removeprefix("menu:")
    if key == "grup":
        await invite_status(update, context)
    elif key == "revendicare":
        await help_topic(update, context, "revendicare")
    elif key in MENU_ANSWERS:
        await query.message.reply_text(MENU_ANSWERS[key], reply_markup=None if key == "om" else answer_keyboard())
    else:
        await show_menu(query.message)


async def cmd_group_redirect(update, context):
    await send_temporary(
        context.bot, update.effective_chat.id, "Îți arăt regulile și linkul tău de invitație în privat:",
        NOTICE_TTL_SECONDS, reply_markup=invite_button(context.bot),
    )


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
    deleted = await delete_member_message(bot, message, message.from_user.id, reason)
    count = await store.add_violation(message.chat_id, message.from_user.id, reason, VIOLATION_WINDOW_HOURS)
    action = violation_action(count)
    if deleted or action != "delete":
        await store.log_event(message.chat_id, message.from_user.id, action, reason, message.message_id)
    if action == "warn":
        await send_temporary(
            bot, message.chat_id,
            f"⚠️ {message.from_user.mention_html()}, mesaj șters ({reason}). Avertisment {count-1}/2 — "
            f"la următoarea abatere primești mute {MUTE_MINUTES} de minute.",
            NOTICE_TTL_SECONDS,
        )
    elif action == "mute":
        until = datetime.now(timezone.utc) + timedelta(minutes=MUTE_MINUTES)
        try:
            await bot.restrict_chat_member(
                message.chat_id, message.from_user.id, READ_ONLY,
                until_date=until, use_independent_chat_permissions=True,
            )
        except Exception:
            pass
        await send_temporary(
            bot, message.chat_id,
            f"🔇 {message.from_user.mention_html()} are mute {MUTE_MINUTES} de minute ({reason}).",
            NOTICE_TTL_SECONDS,
        )


async def group_url(bot):
    if GROUP_INVITE_URL:
        return GROUP_INVITE_URL
    try:
        chat = await bot.get_chat(GROUP_CHAT_ID)
        if getattr(chat, "username", None):
            return f"https://t.me/{chat.username}"
    except Exception:
        pass
    return None


async def ad_reminder_keyboard(bot):
    url = await group_url(bot)
    if url:
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("🚀 DA", url=url),
            InlineKeyboardButton("✕ NU", callback_data="adreminder:no"),
        ]])
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🚀 DA", callback_data="adreminder:yes"),
        InlineKeyboardButton("✕ NU", callback_data="adreminder:no"),
    ]])


async def send_ad_reminder(bot, user_id):
    await bot.send_message(
        user_id,
        "🎉 Poți posta din nou în grup!\n\nVrei să mergi în grup și să postezi?",
        reply_markup=await ad_reminder_keyboard(bot),
    )


async def on_ad_reminder(update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "adreminder:no":
        try:
            await query.message.edit_text("👌 Bine. Poți reveni când vrei să postezi.", reply_markup=None)
        except Exception:
            pass
        return
    url = await group_url(context.bot)
    if url:
        try:
            await query.message.edit_text("🚀 Te așteptăm în grup!", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🚀 Intră în grup", url=url)]]
            ))
        except Exception:
            pass
    else:
        try:
            await query.message.edit_text(
                "🚀 Intră în grup și poți posta din nou.",
                reply_markup=None,
            )
        except Exception:
            pass


async def handle_ad(bot, message):
    user = message.from_user
    count = await store.recent_ad_count(GROUP_CHAT_ID, user.id, AD_WINDOW_HOURS)
    limit = await ad_limit(user.id)
    if count >= limit:
        oldest = await store.oldest_ad(GROUP_CHAT_ID, user.id, AD_WINDOW_HOURS)
        retry = oldest["created_at"] + timedelta(hours=AD_WINDOW_HOURS) if oldest else datetime.now(timezone.utc)
        local_retry = retry.astimezone(LOCAL_TZ).strftime("%d.%m.%Y la %H:%M")
        reason = f"reclame: limita {limit}/24h; următoarea postare permisă la " + local_retry
        if await delete_member_message(bot, message, user.id, reason):
            await store.log_event(GROUP_CHAT_ID, user.id, "delete", reason, message.message_id)
        await store.set_time(f"ad_limit:{GROUP_CHAT_ID}:{user.id}", datetime.now(timezone.utc))
        notice = (f"⛔ {user.mention_html()}, ai atins limita de {limit} {ads_word(limit)} pe zi. "
                  f"Poți posta din nou {friendly_when(retry, datetime.now(timezone.utc))}.")
        if limit < VERIFIED_AD_LIMIT:
            notice += "\n\n" + verification_offer(message.chat.title)
        await send_temporary(bot, GROUP_CHAT_ID, notice, NOTICE_TTL_SECONDS)
        return False

    return True


async def maybe_cleanup():
    global _last_cleanup
    if time.monotonic() - _last_cleanup < 3600:
        return
    _last_cleanup = time.monotonic()
    keep_hours = max(DUPLICATE_COOLDOWN_HOURS, AD_WINDOW_HOURS)
    await store.cleanup(keep_hours)


async def on_group_message(update, context):
    message = update.effective_message
    if not message or message.chat_id != GROUP_CHAT_ID or message.is_automatic_forward:
        return

    bot = context.bot

    if message.from_user and not message.from_user.is_bot:
        await store.set_time("last_human_at", datetime.now(timezone.utc))

    if message.sender_chat:
        if message.sender_chat.id == GROUP_CHAT_ID:
            return
        if await delete_member_message(bot, message, message.sender_chat.id, "postare ca un canal"):
            await store.log_event(
                GROUP_CHAT_ID,
                message.sender_chat.id,
                "delete",
                "postare ca un canal",
                message.message_id,
            )
        return

    user = message.from_user
    if not user or user.is_bot or await is_admin(bot, GROUP_CHAT_ID, user.id):
        return
    if await store.is_whitelisted(GROUP_CHAT_ID, user.id):
        return

    fp = fingerprint_of(message)
    if links_to_approape(fp.urls):
        return  # a link to approape.ro may be posted any time, by anyone in the group

    # External links are advertisements.
    external_link_ad = has_external_link(fp.urls)

    sticker = message.sticker
    is_sticker = bool(sticker and (sticker.is_animated or sticker.is_video))
    is_static_sticker = bool(sticker) and not is_sticker

    async with _moderation_lock:
        member = await store.get_member(GROUP_CHAT_ID, user.id)
        if member is None:
            member = await store.add_member(
                GROUP_CHAT_ID,
                user,
                unlocked=True,
            )

        if sticker and await store.recent_sticker_count(
                GROUP_CHAT_ID, user.id, STICKER_WINDOW_HOURS, static=is_static_sticker) >= STICKERS_PER_DAY:
            # Deleted and explained, but not a violation: no warning, no mute.
            if await delete_member_message(bot, message, user.id, "stickere: limita zilnică"):
                await store.log_event(GROUP_CHAT_ID, user.id, "delete", "stickere: limita zilnică",
                                      message.message_id)
            await send_temporary(
                bot, GROUP_CHAT_ID,
                f"{user.mention_html()}, poți trimite cel mult {STICKERS_PER_DAY} reclame pe zi. "
                f"{REAL_MESSAGES_NOTE}",
                NOTICE_TTL_SECONDS)
            return

        # Repeated text is an ad: the same text (15+ normalized characters) or a reworded
        # copy of it (same_ad_text). Every copy in the last 24h counts, the first one
        # included, so 2 ads/24h means 2 copies. A forward also repeats whatever anyone
        # else posted: that counts as her ad too.
        repeats = [r["id"] for r in await store.recent_texts(GROUP_CHAT_ID, user.id, AD_WINDOW_HOURS)
                   if same_ad_text(fp.text, r["text"])]
        await store.mark_ads(repeats)
        text_ad = bool(repeats) or (
            message.forward_origin is not None and len(fp.text) >= MIN_TEXT_LENGTH_EXACT
            and await store.text_posted_by_others(GROUP_CHAT_ID, user.id, fp.text, AD_WINDOW_HOURS))

        earlier = await store.recent_fingerprints(
            GROUP_CHAT_ID,
            user.id,
            DUPLICATE_COOLDOWN_HOURS,
        )

        is_ad = external_link_ad or text_ad

        if is_ad and not await handle_ad(bot, message):
            return

        # A repeated photo, link or number is a violation, unless the text already made it
        # an ad: that is handled by the ad limit, not punished twice.
        reason = duplicate_reason(fp, earlier)
        if reason and not text_ad:
            await punish(bot, message, f"reclamă repetată: {reason}")
            return

        # GIFs are not rate-limited.
        is_gif = message.animation is not None

        await store.save_message(
            GROUP_CHAT_ID,
            user.id,
            message.message_id,
            fp,
            is_gif,
            is_sticker,
            is_static_sticker,
            is_ad,
        )

        if is_ad:
            new_count = await store.recent_ad_count(
                GROUP_CHAT_ID,
                user.id,
                AD_WINDOW_HOURS,
            )
            await send_temporary(
                bot,
                GROUP_CHAT_ID,
                f"📣 {user.mention_html()}, reclama a fost acceptată. "
                f"Ai folosit {new_count}/{await ad_limit(user.id)} reclame în ultimele 24h.",
                NOTICE_TTL_SECONDS,
            )

        await maybe_cleanup()


async def on_private_message(update, context):
    message, user, bot = update.effective_message, update.effective_user, context.bot
    if not SUPPORT_CHAT_ID:
        await message.reply_text("Momentan nu putem primi mesaje aici. Scrie-ne pe approape.ro.")
        return
    header = await bot.send_message(
        SUPPORT_CHAT_ID,
        f"📩 {user.full_name}" + (f" (@{user.username})" if user.username else "") + f" · id {user.id}\n"
        "Răspunde cu reply la mesajul de mai jos.",
    )
    copy = await bot.copy_message(SUPPORT_CHAT_ID, message.chat_id, message.message_id)
    await store.save_support_thread(header.message_id, user.id)
    await store.save_support_thread(copy.message_id, user.id)
    last = _support_ack_at.get(user.id, 0)
    if time.monotonic() - last > 1800:
        _support_ack_at[user.id] = time.monotonic()
        await message.reply_text("Am primit mesajul. Îți răspundem aici cât de repede putem.")


async def on_support_reply(update, context):
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


async def post_profile(bot, kind, profile):
    caption = posts.profile_caption(kind, profile)
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(profile.photo)
            resp.raise_for_status()
        await bot.send_photo(GROUP_CHAT_ID, photo=resp.content, caption=caption, parse_mode="HTML")
    except Exception:
        await bot.send_message(GROUP_CHAT_ID, caption, parse_mode="HTML")


async def post_question(bot, index):
    question = posts.QUESTIONS[index]
    if "poll" in question:
        await bot.send_poll(GROUP_CHAT_ID, question["poll"], question["options"], is_anonymous=True)
    else:
        await bot.send_message(GROUP_CHAT_ID, question["text"])


async def run_ad_reminders(bot):
    """Tell her she can post again once the ad limit that blocked her lifts."""
    rows = await store.ad_limit_users(GROUP_CHAT_ID, AD_REMINDER_EVERY_DAYS + 1)
    now = datetime.now(timezone.utc)
    for row in rows:
        user_id = row["user_id"]
        last_limit = await store.get_time(f"ad_limit:{GROUP_CHAT_ID}:{user_id}")
        if not last_limit:
            continue
        last_reminder = await store.get_time(f"ad_reminder:{GROUP_CHAT_ID}:{user_id}")
        # One reminder per block, and none if she already had one this week.
        if last_reminder and (last_reminder >= last_limit
                              or now - last_reminder < timedelta(days=AD_REMINDER_EVERY_DAYS)):
            continue
        refused = await store.get_time(f"ad_reminder_refused:{GROUP_CHAT_ID}:{user_id}")
        if refused and refused >= last_limit:
            continue  # Telegram refused this block's reminder; no retry until the next block
        if await store.recent_ad_count(GROUP_CHAT_ID, user_id, AD_WINDOW_HOURS) >= await ad_limit(user_id):
            continue  # still blocked
        try:
            await send_ad_reminder(bot, user_id)
            await store.set_time(f"ad_reminder:{GROUP_CHAT_ID}:{user_id}", now)
        except Forbidden:
            # She never opened the bot privately (or blocked it): bots cannot start a chat.
            await store.set_time(f"ad_reminder_refused:{GROUP_CHAT_ID}:{user_id}", now)
            await store.log_event(GROUP_CHAT_ID, user_id, "reminder_refused", "nu a deschis botul în privat")
        except Exception as exc:
            log.info("Could not send ad reminder to %s: %s", user_id, exc)


async def run_tick(bot):
    async with _tick_lock:
        await run_ad_reminders(bot)
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
                log.warning("Could not read profiles from site: %s", exc)
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


async def cmd_chatid(update, context):
    chat = update.effective_chat
    if chat.type != "private" and not await is_admin(context.bot, chat.id, update.effective_user.id):
        return
    await update.effective_message.reply_text(f"chat id: {chat.id}")


def target_user(message, args):
    replied = message.reply_to_message
    if replied and replied.from_user and not replied.from_user.is_bot and not replied.sender_chat:
        return replied.from_user.id, replied.from_user.mention_html()
    if args and args[0].lstrip("-").isdigit():
        return int(args[0]), f"id {args[0]}"
    return None, None


async def cmd_whitelist(update, context):
    message, bot = update.effective_message, context.bot
    if not await is_admin(bot, GROUP_CHAT_ID, update.effective_user.id):
        return
    user_id, name = target_user(message, context.args)
    adding = message.text.split()[0].split("@")[0].lower() == "/whitelist"
    if user_id is None:
        await admin_reply(update, bot, "Dă reply la un mesaj al persoanei sau scrie id-ul ei după comandă.")
        return
    if adding:
        await store.add_whitelist(GROUP_CHAT_ID, user_id, update.effective_user.id)
        await allow_posting(bot, GROUP_CHAT_ID, user_id)
        await store.log_event(GROUP_CHAT_ID, user_id, "whitelist", f"de {update.effective_user.id}")
        text = f"✅ {name} e pe lista albă: nicio regulă a botului nu i se mai aplică."
    elif await store.remove_whitelist(GROUP_CHAT_ID, user_id):
        await store.log_event(GROUP_CHAT_ID, user_id, "unwhitelist", f"de {update.effective_user.id}")
        text = f"{name} nu mai e pe lista albă: regulele obișnuite i se aplică din nou."
    else:
        text = f"{name} nu era pe lista albă."
    await admin_reply(update, bot, text)


async def cmd_verify(update, context):
    """/verifica marks a member who sent the admin a verification video: she may post
    VERIFIED_AD_LIMIT ads a day instead of AD_LIMIT, and the group is told, permanently.
    /neverifica takes it back, answered only to the admin."""
    message, bot = update.effective_message, context.bot
    if not await is_admin(bot, GROUP_CHAT_ID, update.effective_user.id):
        return
    user_id, name = target_user(message, context.args)
    adding = message.text.split()[0].split("@")[0].lower() == "/verifica"
    if user_id is None:
        await admin_reply(update, bot, "Dă reply la un mesaj al ei cu /verifica, sau scrie id-ul ei după comandă.")
        return
    if name.startswith("id "):
        known = await store.get_member(GROUP_CHAT_ID, user_id)
        if known and known["first_name"]:
            name = html.escape(known["first_name"])
    if adding:
        await store.add_verified(GROUP_CHAT_ID, user_id, update.effective_user.id)
        await store.log_event(GROUP_CHAT_ID, user_id, "verify", f"de {update.effective_user.id}")
        if update.effective_chat.type != "private":
            try:
                await bot.delete_message(update.effective_chat.id, message.message_id)
            except Exception as exc:
                log.warning("Admin command not deleted from the group: %s", exc)
        await bot.send_message(GROUP_CHAT_ID, f"🎥 {name} a fost verificată.", parse_mode="HTML")
        if update.effective_chat.type == "private":
            await message.reply_text(f"Gata, {name} poate posta {VERIFIED_AD_LIMIT} reclame pe zi.", parse_mode="HTML")
        return
    if await store.remove_verified(GROUP_CHAT_ID, user_id):
        await store.log_event(GROUP_CHAT_ID, user_id, "unverify", f"de {update.effective_user.id}")
        text = f"{name} nu mai e verificată: înapoi la {AD_LIMIT} reclame pe zi."
    else:
        text = f"{name} nu era verificată."
    await admin_reply(update, bot, text)


async def cmd_mark_ad(update, context):
    """/reclama, as a reply: a message the rules missed is an ad. It counts toward her daily
    limit as if the bot had caught it, so with no ads left it is deleted with the limit note."""
    message, bot = update.effective_message, context.bot
    if not await is_admin(bot, GROUP_CHAT_ID, update.effective_user.id):
        return
    ad = message.reply_to_message
    if not ad or not ad.from_user or ad.from_user.is_bot or ad.sender_chat:
        await admin_reply(update, bot, "Dă reply cu /reclama la mesajul care e reclamă.")
        return
    user, name = ad.from_user, ad.from_user.mention_html()
    if await is_admin(bot, GROUP_CHAT_ID, user.id) or await store.is_whitelisted(GROUP_CHAT_ID, user.id):
        await admin_reply(update, bot, f"{name} e admin sau pe lista albă: limita de reclame nu i se aplică.")
        return
    async with _moderation_lock:
        saved = await store.find_message(GROUP_CHAT_ID, ad.message_id)
        if saved and saved["is_ad"]:
            text = "Mesajul era deja numărat ca reclamă."
        elif not await handle_ad(bot, ad):
            text = f"{name} nu mai avea reclame azi: mesajul a fost șters, cu nota despre limită."
        else:
            if saved:
                await store.mark_ads([saved["id"]])
            else:
                await store.save_message(GROUP_CHAT_ID, user.id, ad.message_id, fingerprint_of(ad), False, is_ad=True)
            await store.log_event(GROUP_CHAT_ID, user.id, "ad_marked", f"de {update.effective_user.id}", ad.message_id)
            count = await store.recent_ad_count(GROUP_CHAT_ID, user.id, AD_WINDOW_HOURS)
            text = f"📣 Numărat ca reclamă: {name} a folosit {count}/{await ad_limit(user.id)} reclame în ultimele 24h."
    await admin_reply(update, bot, text)


async def cmd_ban(update, context):
    """/ban (reply or id) removes her from the group for good and deletes her messages:
    Telegram's revoke_messages, plus the ones the bot has on record (the last 24h)."""
    message, bot = update.effective_message, context.bot
    if not await is_admin(bot, GROUP_CHAT_ID, update.effective_user.id):
        return
    user_id, name = target_user(message, context.args)
    if user_id is None:
        await admin_reply(update, bot, "Dă reply la un mesaj al persoanei cu /ban, sau scrie id-ul ei după comandă.")
        return
    if await is_admin(bot, GROUP_CHAT_ID, user_id):
        await admin_reply(update, bot, f"{name} e admin: nu o pot bana.")
        return
    try:
        await bot.ban_chat_member(GROUP_CHAT_ID, user_id, revoke_messages=True)
    except Exception as exc:
        log.warning("Ban of %s refused: %s", user_id, exc)
        await admin_reply(update, bot, f"Telegram nu m-a lăsat să o banez pe {name} ({html.escape(str(exc))}). "
                                       "Verifică dacă botul e admin cu dreptul „Ban users”.")
        return
    ids = set(await store.message_ids(GROUP_CHAT_ID, user_id))
    if message.reply_to_message:
        ids.add(message.reply_to_message.message_id)
    ids = sorted(ids)
    for start in range(0, len(ids), 100):  # Telegram takes at most 100 per call
        try:
            await bot.delete_messages(GROUP_CHAT_ID, ids[start:start + 100])
        except Exception as exc:
            log.warning("Messages of banned %s not deleted: %s", user_id, exc)
    await store.log_event(GROUP_CHAT_ID, user_id, "ban", f"de {update.effective_user.id}")
    await admin_reply(update, bot, f"🚫 {name} a fost banată, iar mesajele ei din grup au fost șterse.")


async def admin_reply(update, bot, text):
    """Answer an admin command without the group seeing it: in the group the command is
    deleted and the answer goes to her privately. Telegram lets a bot write first only to
    someone who opened it once, so without that the 60s group notice is the fallback."""
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(text, parse_mode="HTML")
        return
    try:
        await bot.delete_message(update.effective_chat.id, update.effective_message.message_id)
    except Exception as exc:
        log.warning("Admin command not deleted from the group: %s", exc)
    try:
        await bot.send_message(update.effective_user.id, text, parse_mode="HTML")
    except Exception as exc:
        log.info("Admin answer not delivered privately (%s), posting it in the group", exc)
        await send_temporary(bot, GROUP_CHAT_ID, text, NOTICE_TTL_SECONDS)


EVENT_LABELS = {
    "delete": "șters", "warn": "avertisment", "mute": "mute", "unlock": "deblocat",
    "whitelist": "pus pe lista albă", "unwhitelist": "scos de pe lista albă",
    "reminder_refused": "reminder netrimis",
    "verify": "verificată", "unverify": "scoasă de la verificate",
    "delete_failed": "NEȘTERS", "ad_marked": "marcat reclamă de admin", "ban": "banată",
}
NEXT_ACTION_LABELS = {"delete": "ștergere", "warn": "avertisment", "mute": f"mute {MUTE_MINUTES} min"}


def local_when(at, now):
    """'azi la 19:43', 'mâine la 09:10' or '08.10 la 19:43', in Bucharest time."""
    at, today = at.astimezone(LOCAL_TZ), now.astimezone(LOCAL_TZ).date()
    if at.date() == today:
        day = "azi"
    elif at.date() == today + timedelta(days=1):
        day = "mâine"
    else:
        day = at.strftime("%d.%m")
    return f"{day} la {at.strftime('%H:%M')}"


def friendly_when(at, now):
    """'diseară după 19:43', 'mâine dimineață după 09:10', in Bucharest time. The ad
    window is 24h, so the moment is always today or tomorrow."""
    at = at.astimezone(LOCAL_TZ)
    today = at.date() == now.astimezone(LOCAL_TZ).date()
    if at.hour < 5:
        part = "la noapte"
    elif at.hour < 12:
        part = "azi dimineață" if today else "mâine dimineață"
    elif at.hour >= 17:
        part = "diseară" if today else "mâine seară"
    else:
        part = "azi" if today else "mâine"
    return f"{part} după {at.strftime('%H:%M')}"


async def posting_status(bot, user_id, member, ads, now):
    """One line saying whether she can post right now, and if not why and until when."""
    if await is_admin(bot, GROUP_CHAT_ID, user_id):
        return "👑 Admin: poate posta oricând."
    if await store.is_whitelisted(GROUP_CHAT_ID, user_id):
        return "⭐ Pe lista albă: poate posta, nicio regulă nu i se aplică."
    if member is None:
        return "❔ Nu știu dacă e în grup."
    if member.status == "left":
        return "🚪 A ieșit din grup."
    if member.status == "kicked":
        return "🚫 Scoasă din grup (ban)."
    if member.status == "restricted" and not getattr(member, "can_send_messages", True):
        until = getattr(member, "until_date", None)
        return f"🔇 Mute până {local_when(until, now)}." if until else "🔇 Nu poate scrie (restricționată fără termen)."
    if ads >= await ad_limit(user_id):
        oldest = await store.oldest_ad(GROUP_CHAT_ID, user_id, AD_WINDOW_HOURS)
        when = local_when(oldest["created_at"] + timedelta(hours=AD_WINDOW_HOURS), now) if oldest else "în curând"
        return f"⛔ Limita de reclame atinsă: mesaje normale da, reclame din nou {when}."
    return "✅ Poate posta acum."


async def cmd_info(update, context):
    message, bot = update.effective_message, context.bot
    if not await is_admin(bot, GROUP_CHAT_ID, update.effective_user.id):
        return
    user_id, name = target_user(message, context.args)
    if user_id is None:
        await admin_reply(update, bot, "Dă reply la un mesaj al persoanei cu /info, sau scrie id-ul ei după comandă.")
        return
    now = datetime.now(timezone.utc)
    try:
        member = await bot.get_chat_member(GROUP_CHAT_ID, user_id)
    except Exception:
        member = None
    known = await store.get_member(GROUP_CHAT_ID, user_id)
    if name.startswith("id ") and known:
        name = html.escape(known["first_name"] or "")
    if known and known["username"]:
        name += f" @{html.escape(known['username'])}"
    ads = await store.recent_ad_count(GROUP_CHAT_ID, user_id, AD_WINDOW_HOURS)
    static = await store.recent_sticker_count(GROUP_CHAT_ID, user_id, STICKER_WINDOW_HOURS, static=True)
    animated = await store.recent_sticker_count(GROUP_CHAT_ID, user_id, STICKER_WINDOW_HOURS, static=False)
    violations = await store.violation_count(GROUP_CHAT_ID, user_id, VIOLATION_WINDOW_HOURS)
    next_action = NEXT_ACTION_LABELS[violation_action(violations + 1)]
    invites = await store.invite_count(GROUP_CHAT_ID, user_id)

    lines = [f"ℹ️ {name} · id {user_id}", await posting_status(bot, user_id, member, ads, now), ""]
    if await store.is_verified(GROUP_CHAT_ID, user_id):
        lines.append(f"🎥 Verificată: {VERIFIED_AD_LIMIT} reclame pe zi")
    lines.append(f"📣 Reclame ({AD_WINDOW_HOURS}h): {ads}/{await ad_limit(user_id)}")
    lines.append(f"🎨 Stickere ({STICKER_WINDOW_HOURS}h): statice {static}/{STICKERS_PER_DAY} · "
                 f"animate {animated}/{STICKERS_PER_DAY}")
    lines.append(f"⚠️ Abateri ({VIOLATION_WINDOW_HOURS:g}h): {violations} → următoarea: {next_action}")
    joined = f" · în grup din {known['first_seen'].astimezone(LOCAL_TZ):%d.%m}" if known else ""
    lines.append(f"👥 A adus: {invites}{joined}")
    reminder = await store.get_time(f"ad_reminder:{GROUP_CHAT_ID}:{user_id}")
    if reminder:
        lines.append(f"🔔 Reminder „poți posta”: trimis {local_when(reminder, now)}")

    events = await store.recent_events(GROUP_CHAT_ID, user_id, 5)
    if events:
        lines += ["", "Ultimele acțiuni:"]
        for event in events:
            label = EVENT_LABELS.get(event["event_type"], event["event_type"])
            reason = (event["reason"] or "").split(";")[0]
            lines.append(f"• {event['created_at'].astimezone(LOCAL_TZ):%d.%m %H:%M} {label}"
                         + (f": {html.escape(reason)}" if reason else ""))
    await admin_reply(update, bot, "\n".join(lines))


async def cmd_unlock(update, context):
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
    await allow_posting(bot, GROUP_CHAT_ID, user_id)
    await store.log_event(GROUP_CHAT_ID, user_id, "unlock", f"manual, de {update.effective_user.id}")
    await admin_reply(update, bot, f"✅ {name} poate posta din nou (mute ridicat). Regulile i se aplică în continuare.")


async def cmd_stats(update, context):
    if not await is_admin(context.bot, GROUP_CHAT_ID, update.effective_user.id):
        return
    s = await store.stats(GROUP_CHAT_ID)
    await admin_reply(
        update, context.bot,
        f"📊 Membri urmăriți: {s['members']}\n"
        f"Invitații: {s['invites']}\nAbateri (7 zile): {s['violations_7d']}"
    )


def build_application():
    application = Application.builder().token(BOT_TOKEN).updater(None).build()
    private = filters.ChatType.PRIVATE
    group = filters.Chat(GROUP_CHAT_ID) if GROUP_CHAT_ID else filters.ChatType.GROUPS
    application.add_handler(CommandHandler("chatid", cmd_chatid))
    application.add_handler(CommandHandler("stats", cmd_stats, filters=group))
    application.add_handler(CommandHandler(["whitelist", "unwhitelist"], cmd_whitelist, filters=group))
    application.add_handler(CommandHandler("info", cmd_info, filters=group | private))
    application.add_handler(CommandHandler(["verifica", "neverifica"], cmd_verify, filters=group | private))
    application.add_handler(CommandHandler("unlock", cmd_unlock, filters=group | private))
    application.add_handler(CommandHandler("reclama", cmd_mark_ad, filters=group))
    application.add_handler(CommandHandler("ban", cmd_ban, filters=group | private))
    application.add_handler(CommandHandler("start", cmd_start, filters=private))
    application.add_handler(CallbackQueryHandler(on_ad_reminder, pattern=r"^adreminder:"))
    application.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    application.add_handler(CommandHandler(["invite", "status"], invite_status, filters=private))
    application.add_handler(CommandHandler(["invite", "status"], cmd_group_redirect, filters=group))
    if SUPPORT_CHAT_ID:
        support = filters.Chat(SUPPORT_CHAT_ID)
        application.add_handler(MessageHandler(support & ~filters.COMMAND, on_support_reply))
    application.add_handler(MessageHandler(private & ~filters.COMMAND, on_private_message))
    application.add_handler(ChatMemberHandler(on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(MessageHandler(group & ~filters.StatusUpdate.ALL, on_group_message), group=1)
    return application


app = FastAPI()
tg_app = None


@app.get("/")
async def health():
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
    received = request.headers.get("X-Tick-Secret", "")
    if not TICK_SECRET or not GROUP_CHAT_ID or not hmac.compare_digest(received, TICK_SECRET):
        return Response(status_code=403)
    try:
        await run_tick(tg_app.bot)
    except Exception:
        log.exception("Scheduled task failed")
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
    if GROUP_CHAT_ID:
        spawn(lift_invite_locks(tg_app.bot))
    if WEBHOOK_URL:
        await tg_app.bot.set_webhook(
            url=f"{WEBHOOK_URL}/telegram/webhook",
            secret_token=WEBHOOK_SECRET,
            allowed_updates=["message", "chat_member", "callback_query"],
        )
        log.info("Webhook set")
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=PORT, log_level="info"))
    try:
        await server.serve()
    finally:
        await tg_app.stop()
        await tg_app.shutdown()
        await store.close()


if __name__ == "__main__":
    asyncio.run(main())
