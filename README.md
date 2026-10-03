# Telegram Group Bot — approape.ro

Moderation and growth bot for the approape.ro Telegram group, plus a help desk in the
bot's private chat.

## What it does

**Posting rights through invites**
- New members are read-only until **3 people** join the group through their personal link.
- Each person has **one** personal link. They get it in the private chat with the bot:
  a button in the welcome message, or `/invite` in the private chat. A read-only member
  cannot type anything in the group, so the link can only be delivered there.
- Only joins through the bot's personal links count. A member added by hand, a rejoin,
  yourself, or a bot never counts. A person counts once, for whoever brought them first.
- An invite counts **for good**, even if the invited person leaves later.
- Members who were in the group **before** the bot need their 3 invites too (marked
  `legacy`): their first post is deleted, they are restricted and shown their link.
- An unlocked member who leaves and comes back can post again straight away.

**Moderation** (for everyone except the group's admins, read live from Telegram)
- A message containing a link to **approape.ro** (or a subdomain, including a link hidden
  behind text) is exempt from every rule below and from the invite requirement. A member
  Telegram has already restricted still cannot send anything, though.
- Repeated ad within 6 hours: the same photo/clip, any shared link (including links
  hidden behind text), the same Romanian mobile number, the same text, or a near-identical
  text (≥ 40 characters).
- At most 1 GIF message per 60 seconds (a burst delivered all at once still counts).
- At most 1 message with animated/video stickers per 24 hours. The next one is deleted with
  a note recommending an approape.ro account; it is not a violation (no warning, no mute).
  Static stickers are not limited.
- Escalation within 48h: 1st violation = delete only; 2nd and 3rd = delete + warning;
  4th onwards = delete + mute for 60 minutes.
- Messages posted "as a channel" are deleted; anonymous admins and posts from the linked
  channel are left alone.

Every action (delete, warn, mute, unlock) is logged in `moderation_events` with its reason
and kept for good, so "why was she muted?" can still be answered weeks later:

    SELECT created_at, event_type, reason FROM moderation_events
    WHERE user_id = <id> ORDER BY created_at DESC;

**Help desk**
- `https://t.me/<bot>?start=ajutor` opens the private chat with a help message. That's the
  link the site uses wherever it says "scrie-ne pe Telegram".
- Anything written to the bot privately reaches `SUPPORT_CHAT_ID`. An admin's **reply**
  to that message goes back to the person.

## Commands

| Command | Where | Who |
|---|---|---|
| `/start`, `/invite`, `/status` | private | anyone — personal link + progress |
| `/invite`, `/status` | group | button to the private chat |
| `/stats` | group | admins |
| `/chatid` | anywhere | group admins / anyone in private |

## Setup

1. **BotFather:** create the bot and keep the token.
2. **Group:** it must be a supergroup. Add the bot as an administrator with: *Delete messages*,
   *Ban/restrict users*, *Invite users via link*.
3. **Supabase:** create a project. Under *Connect*, copy the **Session pooler** or
   **Transaction pooler** URI, not *Direct connection* (that one is IPv6-only and Render
   cannot reach it). The bot creates its own tables on startup.
4. **Render:** a Web Service from this repo (Docker), with the variables below.
5. Send `/chatid` in the group and in the help chat (an admin group, or your private chat
   with the bot), set `GROUP_CHAT_ID` / `SUPPORT_CHAT_ID`, then redeploy.

### Environment variables

Required:

    BOT_TOKEN=token from BotFather
    DATABASE_URL=postgresql://...pooler.supabase.com:6543/postgres
    WEBHOOK_SECRET=at least 16 random characters
    GROUP_CHAT_ID=-100...        (from /chatid)
    SUPPORT_CHAT_ID=...          (from /chatid)

Optional (defaults shown):

    INVITES_REQUIRED=3
    DUPLICATE_COOLDOWN_HOURS=6
    GIF_WINDOW_SECONDS=60
    GIF_MAX_IN_WINDOW=1
    SIMILARITY_THRESHOLD=0.92
    VIOLATION_WINDOW_HOURS=48
    MUTE_MINUTES=60
    PORT=10000
    WEBHOOK_URL=https://...      (not needed on Render: RENDER_EXTERNAL_URL is used)

## Render Free

The service sleeps after ~15 minutes without traffic and wakes on the next update from
Telegram (first response ~30–60s; Telegram retries in the meantime). The webhook is set on
every start and **never deleted** on shutdown, otherwise the bot would never wake up.
All state lives in Postgres, so restarts and redeploys lose nothing. Note: a Supabase
project with no activity for 7 days gets paused; an active group keeps it awake.

## Tests

    pip install -r requirements-dev.txt
    docker run -d --rm --name tgbot-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16-alpine
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres pytest

Without `TEST_DATABASE_URL`, only the pure moderation tests run.
