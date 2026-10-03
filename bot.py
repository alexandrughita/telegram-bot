import asyncio
import hmac
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import uvicorn
from fastapi import FastAPI, Request, Response
from telegram import ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity, Update
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters

from db import Store
from moderation import build_fingerprint, duplicate_reason, violation_action

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ.get("DATABASE_URL", "")
# Render sets RENDER_EXTERNAL_URL itself, so WEBHOOK_URL is only needed elsewhere.
WEBHOOK_URL = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

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
_support_ack_at = {}     # user_id -> last time we acknowledged a help message
_background = set()      # strong refs to fire-and-forget tasks
# Telegram delivers updates over parallel webhook requests (a whole backlog at once
# when Render wakes up). Each check reads what earlier messages saved, so group
# messages are moderated one at a time or a burst slips through unchecked.
_moderation_lock = asyncio.Lock()
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


async def maybe_unlock(bot, inviter_id):
    member = await store.get_member(GROUP_CHAT_ID, inviter_id)
    if not member or member["unlocked"]:
        return
    if await store.invite_count(GROUP_CHAT_ID, inviter_id) < INVITES_REQUIRED:
        return
    await store.set_unlocked(GROUP_CHAT_ID, inviter_id)
    await allow_posting(bot, GROUP_CHAT_ID, inviter_id)
    await store.log_event(GROUP_CHAT_ID, inviter_id, "unlock", f"{INVITES_REQUIRED} invitații")
    try:
        await bot.send_message(inviter_id, "✅ Ai adus 3 membri — acum poți posta în grup.")
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

    existing = await store.get_member(GROUP_CHAT_ID, user.id)
    if existing:
        # Coming back: restore what she had, and no invite credit for anyone.
        if existing["unlocked"]:
            await allow_posting(bot, GROUP_CHAT_ID, user.id)
        else:
            await restrict(bot, GROUP_CHAT_ID, user.id)
        return

    await store.add_member(GROUP_CHAT_ID, user)
    await restrict(bot, GROUP_CHAT_ID, user.id)

    # Only the bot's personal links count. Someone added by hand, or through
    # a link the bot did not create, is credited to nobody.
    if cm.invite_link:
        inviter_id = await store.inviter_for_link(GROUP_CHAT_ID, cm.invite_link.invite_link)
        if inviter_id and inviter_id != user.id:
            if await store.record_invite(GROUP_CHAT_ID, user.id, inviter_id):
                await maybe_unlock(bot, inviter_id)

    await send_temporary(
        bot, GROUP_CHAT_ID,
        f"Bun venit, {user.mention_html()}! Ca să poți posta, adu {INVITES_REQUIRED} membri "
        f"prin linkul tău personal.",
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

    member = await store.get_member(GROUP_CHAT_ID, user.id)
    if member is None:
        # In the group, but the bot never saw her join: she was there before it.
        # She still needs her invites, like everyone else.
        member = await store.add_member(GROUP_CHAT_ID, user, legacy=True)

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
        state = f"Mai ai nevoie de {INVITES_REQUIRED - count} ca să poți posta."
    await update.effective_message.reply_text(
        f"🔗 Linkul tău:\n{link}\n\n👥 Invitații: {min(count, INVITES_REQUIRED)}/{INVITES_REQUIRED}\n{state}",
        disable_web_page_preview=True)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0] == "ajutor":
        await update.effective_message.reply_text(
            "Scrie-ne aici cu ce te putem ajuta. Echipa approape.ro îți răspunde în această conversație.")
        return
    await invite_status(update, context)


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
    keep_hours = max(DUPLICATE_COOLDOWN_HOURS, GIF_WINDOW_SECONDS / 3600)
    await store.cleanup(keep_hours)


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or message.chat_id != GROUP_CHAT_ID or message.is_automatic_forward:
        return  # automatic forwards are posts from the group's linked channel
    bot = context.bot

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

    async with _moderation_lock:
        member = await store.get_member(GROUP_CHAT_ID, user.id)
        if member is None:
            # The bot never saw her join, so she was in the group before it. She still
            # needs her invites; Telegram has not restricted her yet, so that happens below.
            member = await store.add_member(GROUP_CHAT_ID, user, legacy=True)
        if not member["unlocked"]:
            try:
                await bot.delete_message(message.chat_id, message.message_id)
            except Exception:
                pass
            await store.log_event(GROUP_CHAT_ID, user.id, "delete", "fără drept de postare", message.message_id)
            await restrict(bot, GROUP_CHAT_ID, user.id)
            await send_temporary(
                bot, GROUP_CHAT_ID,
                f"{user.mention_html()}, ca să poți posta, adu {INVITES_REQUIRED} membri "
                f"prin linkul tău personal.",
                NOTICE_TTL_SECONDS, reply_markup=invite_button(bot))
            return

        fp = fingerprint_of(message)
        # Animated and video stickers are used exactly like GIFs; static ones are not.
        sticker = message.sticker
        is_gif = message.animation is not None or bool(sticker and (sticker.is_animated or sticker.is_video))
        if is_gif and await store.recent_gif_count(GROUP_CHAT_ID, user.id, GIF_WINDOW_SECONDS) >= GIF_MAX_IN_WINDOW:
            await punish(bot, message, "prea multe GIF-uri la rând")
            return

        earlier = await store.recent_fingerprints(GROUP_CHAT_ID, user.id, DUPLICATE_COOLDOWN_HOURS)
        reason = duplicate_reason(fp, earlier, SIMILARITY_THRESHOLD)
        if reason:
            await punish(bot, message, f"reclamă repetată: {reason}")
            return

        await store.save_message(GROUP_CHAT_ID, user.id, message.message_id, fp, is_gif)
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
# Admin commands
# ------------------------------------------------------------
async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    log.warning("GROUP CHAT ID DETECTED: %s", chat.id)
    if chat.type != "private" and not await is_admin(context.bot, chat.id, update.effective_user.id):
        return
    await update.effective_message.reply_text(f"chat id: {chat.id}")


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
    application.add_handler(CommandHandler("start", cmd_start, filters=private))
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
    return {"ok": True, "service": "telegram-group-bot"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(received, WEBHOOK_SECRET):
        return Response(status_code=403)
    update = Update.de_json(await request.json(), tg_app.bot)
    await tg_app.process_update(update)
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
            allowed_updates=["message", "chat_member"])
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
