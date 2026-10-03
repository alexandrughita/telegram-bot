import os
import re
import sqlite3
import hashlib
import difflib
import logging
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

from fastapi import FastAPI, Request
import uvicorn

from telegram import Update, ChatPermissions
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, ChatMemberHandler, filters,
)

# ------------------------------------------------------------
# Configuration
# ------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me")

INVITES_REQUIRED = int(os.environ.get("INVITES_REQUIRED", "3"))
DUPLICATE_COOLDOWN_HOURS = float(os.environ.get("DUPLICATE_COOLDOWN_HOURS", "6"))
GIF_WINDOW_SECONDS = int(os.environ.get("GIF_WINDOW_SECONDS", "60"))
GIF_MAX_IN_WINDOW = int(os.environ.get("GIF_MAX_IN_WINDOW", "1"))
MAX_ADS_PER_DAY = int(os.environ.get("MAX_ADS_PER_DAY", "0"))  # 0 = disabled
SIMILARITY_THRESHOLD = float(os.environ.get("SIMILARITY_THRESHOLD", "0.92"))
AUTO_MUTE_AFTER = int(os.environ.get("AUTO_MUTE_AFTER", "3"))
AUTO_MUTE_MINUTES = int(os.environ.get("AUTO_MUTE_MINUTES", "60"))

DB_PATH = os.environ.get("DB_PATH", "bot.db")
PORT = int(os.environ.get("PORT", "10000"))

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("telegram-bot")

app = FastAPI()
tg_app = Application.builder().token(BOT_TOKEN).build()


# ------------------------------------------------------------
# Database
# ------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        invited_by INTEGER,
        joined_at TEXT,
        unlocked INTEGER DEFAULT 0,
        is_admin INTEGER DEFAULT 0,
        warnings INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS invite_links (
        chat_id INTEGER NOT NULL,
        invite_link TEXT PRIMARY KEY,
        inviter_id INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS invites (
        chat_id INTEGER NOT NULL,
        invited_user_id INTEGER NOT NULL,
        inviter_id INTEGER NOT NULL,
        invite_link TEXT,
        joined_at TEXT NOT NULL,
        PRIMARY KEY (chat_id, invited_user_id)
    );

    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        text_hash TEXT,
        normalized_text TEXT,
        photo_unique_id TEXT,
        url TEXT,
        is_gif INTEGER DEFAULT 0,
        created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS warnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        reason TEXT,
        created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_messages_user_time
      ON messages(chat_id, user_id, created_at);

    CREATE INDEX IF NOT EXISTS idx_invites_inviter
      ON invites(chat_id, inviter_id);
    """)
    conn.commit()
    conn.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value):
    return datetime.fromisoformat(value)


def upsert_user(chat_id, user, invited_by=None):
    conn = db()
    conn.execute("""
        INSERT INTO users(chat_id,user_id,username,first_name,invited_by,joined_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(chat_id,user_id) DO UPDATE SET
          username=excluded.username,
          first_name=excluded.first_name
    """, (
        chat_id, user.id, user.username, user.first_name,
        invited_by, now_iso()
    ))
    conn.commit()
    conn.close()


def is_unlocked(chat_id, user_id):
    conn = db()
    row = conn.execute(
        "SELECT unlocked FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    ).fetchone()
    conn.close()
    return bool(row and row["unlocked"])


def is_admin(chat_id, user_id):
    conn = db()
    row = conn.execute(
        "SELECT is_admin FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    ).fetchone()
    conn.close()
    return bool(row and row["is_admin"])


def invite_count(chat_id, inviter_id):
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM invites WHERE chat_id=? AND inviter_id=?",
        (chat_id, inviter_id)
    ).fetchone()
    conn.close()
    return int(row["n"])


def add_warning(chat_id, user_id, reason):
    conn = db()
    conn.execute(
        "INSERT INTO warnings(chat_id,user_id,reason,created_at) VALUES(?,?,?,?)",
        (chat_id, user_id, reason, now_iso())
    )
    conn.execute(
        "UPDATE users SET warnings=warnings+1 WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    )
    row = conn.execute(
        "SELECT warnings FROM users WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    ).fetchone()
    conn.commit()
    conn.close()
    return int(row["warnings"]) if row else 1


# ------------------------------------------------------------
# Telegram permissions / membership
# ------------------------------------------------------------
READ_ONLY_PERMISSIONS = ChatPermissions(
    can_send_messages=False,
    can_send_audios=False,
    can_send_documents=False,
    can_send_photos=False,
    can_send_videos=False,
    can_send_video_notes=False,
    can_send_voice_notes=False,
    can_send_polls=False,
    can_send_other_messages=False,
    can_add_web_page_previews=False,
    can_change_info=False,
    can_invite_users=True,
    can_pin_messages=False,
    can_manage_topics=False,
)

POSTING_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_invite_users=True,
    can_pin_messages=False,
    can_manage_topics=False,
)


async def restrict_user(chat_id, user_id):
    try:
        await tg_app.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=READ_ONLY_PERMISSIONS,
        )
    except Exception as exc:
        log.warning("Could not restrict %s: %s", user_id, exc)


async def unlock_user(chat_id, user_id):
    try:
        await tg_app.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=POSTING_PERMISSIONS,
        )
    except Exception as exc:
        log.warning("Could not unlock %s: %s", user_id, exc)


# ------------------------------------------------------------
# Invite system
# ------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_user:
        return

    chat = update.effective_chat
    user = update.effective_user

    if chat.type == "private":
        await update.message.reply_text(
            "Adaugă-mă în grup ca administrator și folosește /invite în grup."
        )
        return

    await status(update, context)


async def invite(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    user = update.effective_user

    try:
        link = await tg_app.bot.create_chat_invite_link(
            chat_id=chat_id,
            name=f"invite-{user.id}",
            creates_join_request=False,
        )
    except Exception as exc:
        await update.message.reply_text(
            "Nu pot crea linkul personal. Botul trebuie să fie administrator "
            "și să aibă dreptul de a invita utilizatori."
        )
        log.exception("create_chat_invite_link failed: %s", exc)
        return

    conn = db()
    conn.execute(
        "INSERT OR REPLACE INTO invite_links(chat_id,invite_link,inviter_id,created_at)"
        " VALUES(?,?,?,?)",
        (chat_id, link.invite_link, user.id, now_iso())
    )
    conn.commit()
    conn.close()

    count = invite_count(chat_id, user.id)
    remaining = max(0, INVITES_REQUIRED - count)

    await update.message.reply_text(
        f"🔗 Linkul tău personal:\n{link.invite_link}\n\n"
        f"👥 Invitații validați: {count}/{INVITES_REQUIRED}\n"
        f"{'✅ Poți posta.' if remaining == 0 else f'Îți mai trebuie {remaining}.'}"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or not update.effective_user:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    count = invite_count(chat_id, user_id)
    unlocked = is_unlocked(chat_id, user_id)

    if unlocked:
        text = "✅ Ai drept de postare."
    else:
        text = (
            f"🔒 Nu ai încă drept de postare.\n"
            f"Invitații validați: {count}/{INVITES_REQUIRED}\n"
            f"Folosește /invite pentru linkul tău."
        )
    await update.message.reply_text(text)


async def handle_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.chat_member
    if not cm:
        return

    chat_id = cm.chat.id
    new = cm.new_chat_member
    old = cm.old_chat_member
    user = new.user

    # Ignore bots joining.
    if user.is_bot:
        return

    joined = (
        old.status in ("left", "kicked")
        and new.status in ("member", "restricted")
    )
    if not joined:
        return

    inviter_id = None
    invite_link = getattr(cm, "invite_link", None)
    invite_url = invite_link.invite_link if invite_link else None

    if invite_url:
        conn = db()
        row = conn.execute(
            "SELECT inviter_id FROM invite_links WHERE chat_id=? AND invite_link=?",
            (chat_id, invite_url)
        ).fetchone()
        conn.close()
        if row:
            inviter_id = row["inviter_id"]

    upsert_user(chat_id, user, inviter_by=inviter_id)

    if inviter_id and inviter_id != user.id:
        conn = db()
        conn.execute(
            "INSERT OR IGNORE INTO invites(chat_id,invited_user_id,inviter_id,invite_link,joined_at)"
            " VALUES(?,?,?,?,?)",
            (chat_id, user.id, inviter_id, invite_url, now_iso())
        )
        conn.commit()
        conn.close()

        count = invite_count(chat_id, inviter_id)
        if count >= INVITES_REQUIRED:
            conn = db()
            conn.execute(
                "UPDATE users SET unlocked=1 WHERE chat_id=? AND user_id=?",
                (chat_id, inviter_id)
            )
            conn.commit()
            conn.close()
            await unlock_user(chat_id, inviter_id)

    # New members are read-only until unlocked.
    await restrict_user(chat_id, user.id)


# ------------------------------------------------------------
# Moderation helpers
# ------------------------------------------------------------
def normalize_text(text):
    text = text.lower()
    text = re.sub(r"https?://\S+", " URL ", text)
    text = re.sub(r"[@#]\w+", " TAG ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    return text.strip()


def extract_first_url(text):
    if not text:
        return None
    m = re.search(r"https?://[^\s]+", text)
    if not m:
        return None
    return m.group(0).rstrip(").,!?;:")


def photo_unique_id(message):
    if not message.photo:
        return None
    # Telegram sends several sizes; the largest normally has the last item.
    return message.photo[-1].file_unique_id


def message_has_gif(message):
    return bool(message.animation)


def recent_gif_count(chat_id, user_id):
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=GIF_WINDOW_SECONDS)
    conn = db()
    rows = conn.execute(
        "SELECT created_at FROM messages "
        "WHERE chat_id=? AND user_id=? AND is_gif=1",
        (chat_id, user_id)
    ).fetchall()
    conn.close()
    return sum(parse_iso(r["created_at"]) >= cutoff for r in rows)


def check_duplicate(chat_id, user_id, normalized, photo_id, url):
    if not normalized and not photo_id and not url:
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(hours=DUPLICATE_COOLDOWN_HOURS)
    conn = db()
    rows = conn.execute(
        "SELECT normalized_text,photo_unique_id,url FROM messages "
        "WHERE chat_id=? AND user_id=? AND created_at>=?",
        (chat_id, user_id, cutoff.isoformat())
    ).fetchall()
    conn.close()

    for row in rows:
        if photo_id and row["photo_unique_id"] == photo_id:
            return True

        if url and row["url"] == url:
            # Same URL is considered the same promotion.
            return True

        old = row["normalized_text"]
        if normalized and old:
            if normalized == old:
                return True
            # Avoid calling short texts "similar".
            if len(normalized) >= 40 and len(old) >= 40:
                ratio = difflib.SequenceMatcher(None, normalized, old).ratio()
                if ratio >= SIMILARITY_THRESHOLD:
                    return True

    return False


def save_message(chat_id, user_id, message_id, normalized, photo_id, url, is_gif):
    digest = hashlib.sha256(
        f"{normalized}|{photo_id}|{url}".encode("utf-8")
    ).hexdigest()

    conn = db()
    conn.execute(
        "INSERT INTO messages(chat_id,user_id,message_id,text_hash,normalized_text,"
        "photo_unique_id,url,is_gif,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            chat_id, user_id, message_id, digest, normalized,
            photo_id, url, int(is_gif), now_iso()
        )
    )
    conn.commit()
    conn.close()


async def delete_message(message, reason, warn=False):
    chat_id = message.chat_id
    user_id = message.from_user.id

    try:
        await message.delete()
    except Exception as exc:
        log.warning("Delete failed: %s", exc)

    if warn:
        warnings = add_warning(chat_id, user_id, reason)
        if warnings >= AUTO_MUTE_AFTER:
            try:
                until = datetime.now(timezone.utc) + timedelta(minutes=AUTO_MUTE_MINUTES)
                await tg_app.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=READ_ONLY_PERMISSIONS,
                    until_date=until,
                )
            except Exception as exc:
                log.warning("Auto mute failed: %s", exc)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.from_user or not update.effective_chat:
        return

    # Ignore channel posts / anonymous admin messages for now.
    user = message.from_user
    if user.is_bot:
        return

    chat_id = message.chat_id
    user_id = user.id

    # Admins are allowed through the filters.
    if is_admin(chat_id, user_id):
        return

    # Users who aren't unlocked should not normally be able to send messages.
    # If Telegram delivered one before restriction was applied, delete it.
    if not is_unlocked(chat_id, user_id):
        await delete_message(message, "posting before unlock", warn=False)
        return

    text = message.text or message.caption or ""
    normalized = normalize_text(text)
    url = extract_first_url(text)
    photo_id = photo_unique_id(message)
    is_gif = message_has_gif(message)

    # Telegram represents a GIF as an Animation. A single message can contain
    # only one animation, so "max 1 GIF/message" is effectively enforced by
    # Telegram itself. We additionally prevent GIF spam across consecutive
    # messages.
    if is_gif and recent_gif_count(chat_id, user_id) >= GIF_MAX_IN_WINDOW:
        await delete_message(message, "too many GIFs", warn=True)
        return

    # Duplicate / near-duplicate promotion.
    if check_duplicate(chat_id, user_id, normalized, photo_id, url):
        await delete_message(message, "duplicate promotion", warn=True)
        return

    save_message(
        chat_id, user_id, message.message_id,
        normalized, photo_id, url, is_gif
    )


# ------------------------------------------------------------
# Admin commands
# ------------------------------------------------------------
async def admin_check(update: Update):
    if not update.effective_chat or not update.effective_user:
        return False

    member = await tg_app.bot.get_chat_member(
        update.effective_chat.id, update.effective_user.id
    )
    return member.status in ("administrator", "creator")


async def setup_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return

    chat_id = update.effective_chat.id
    user = update.effective_user
    conn = db()
    conn.execute(
        "INSERT INTO users(chat_id,user_id,username,first_name,is_admin,unlocked,joined_at)"
        " VALUES(?,?,?,?,1,1,?) "
        "ON CONFLICT(chat_id,user_id) DO UPDATE SET is_admin=1,unlocked=1",
        (chat_id, user.id, user.username, user.first_name, now_iso())
    )
    conn.commit()
    conn.close()
    await update.message.reply_text("👑 Ești setat ca admin al botului.")


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_check(update):
        return

    chat_id = update.effective_chat.id
    conn = db()
    members = conn.execute(
        "SELECT COUNT(*) n FROM users WHERE chat_id=?", (chat_id,)
    ).fetchone()["n"]
    unlocked = conn.execute(
        "SELECT COUNT(*) n FROM users WHERE chat_id=? AND unlocked=1", (chat_id,)
    ).fetchone()["n"]
    invites = conn.execute(
        "SELECT COUNT(*) n FROM invites WHERE chat_id=?", (chat_id,)
    ).fetchone()["n"]
    warnings = conn.execute(
        "SELECT COUNT(*) n FROM warnings WHERE chat_id=?", (chat_id,)
    ).fetchone()["n"]
    conn.close()

    await update.message.reply_text(
        f"📊 Stats\n\n"
        f"Members tracked: {members}\n"
        f"Unlocked: {unlocked}\n"
        f"Invites: {invites}\n"
        f"Warnings: {warnings}"
    )


# ------------------------------------------------------------
# FastAPI webhook
# ------------------------------------------------------------
@app.get("/")
async def health():
    return {"ok": True, "service": "telegram-group-bot"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    # Telegram's secret_token is sent as this header.
    if WEBHOOK_SECRET:
        received = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if received != WEBHOOK_SECRET:
            return {"ok": False}

    data = await request.json()
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return {"ok": True}


async def on_startup():
    init_db()

    if not WEBHOOK_URL:
        log.warning("WEBHOOK_URL is not set. Webhook will not be registered.")
        return

    url = f"{WEBHOOK_URL}/telegram/webhook"
    await tg_app.bot.set_webhook(
        url=url,
        secret_token=WEBHOOK_SECRET,
        allowed_updates=[
            "message",
            "chat_member",
        ],
    )
    log.info("Webhook set: %s", url)


async def on_shutdown():
    try:
        await tg_app.bot.delete_webhook()
    except Exception:
        pass


# Handlers
tg_app.add_handler(CommandHandler("start", start))
tg_app.add_handler(CommandHandler("invite", invite))
tg_app.add_handler(CommandHandler("status", status))
tg_app.add_handler(CommandHandler("setupadmin", setup_admin))
tg_app.add_handler(CommandHandler("stats", stats))
tg_app.add_handler(
    ChatMemberHandler(handle_member_update, ChatMemberHandler.CHAT_MEMBER)
)
tg_app.add_handler(
    MessageHandler(filters.ALL & ~filters.COMMAND, handle_message)
)


async def main():
    await tg_app.initialize()
    await tg_app.start()
    await on_startup()

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        await on_shutdown()
        await tg_app.stop()
        await tg_app.shutdown()


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
