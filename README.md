# Telegram Group Bot — approape.ro

Moderation and growth bot for the approape.ro Telegram group, plus a help desk in the
bot's private chat.

## What it does

**Everyone can post** (the 3-invite rule was removed on 2026-10-05)
- New members and members from before the bot can post straight away. Members the old
  rule left read-only are lifted once, on the first start after the change
  (`lift_invite_locks`, logged as `unlock` in `moderation_events`).
- Invites are still counted, for growth only: each person has one personal link
  (`/invite` in the private chat), and a first join through it, or being added by hand
  ("Add members"), counts for whoever brought her. A rejoin, yourself or a bot never counts.

**Whitelist**
- An admin replies to someone's message with `/whitelist` (or writes `/whitelist <id>`, for
  someone who cannot post): no rule of the bot applies to her any more, and any mute she
  is under is lifted. She does not become an
  admin. `/unwhitelist` brings the normal rules back. Both are logged in `moderation_events`.

**Moderation** (for everyone except the group's admins, read live from Telegram)
- **Ads: at most 2 per 24 hours.** An ad is a message with a link outside approape.ro, or
  the same text (≥ 15 characters after normalising) posted again within 24 hours — every
  copy counts, the first one included, so the same text can appear twice a day. The third
  is deleted with a note saying when she can post again; it is not a violation. A week
  after she hit the limit she gets a private reminder that she can post again.
- Other repeats within 6 hours are violations: the same photo/clip, the same link
  (including links hidden behind text), the same Romanian mobile number, or a
  near-identical text (≥ 40 characters).
- Stickers: at most 2 static and 2 animated/video per 24 hours, counted separately (a burst
  delivered all at once still counts). The next one is deleted with a note asking for a
  real message; it is not a violation.
- GIFs are not limited.
- Escalation within 48h: 1st violation = delete only; 2nd and 3rd = delete + warning;
  4th onwards = delete + mute for 60 minutes.
- Messages posted "as a channel" are deleted; anonymous admins and posts from the linked
  channel are left alone.

Every action (delete, warn, mute, unlock) is logged in `moderation_events` with its reason
and kept for good, so "why was she muted?" can still be answered weeks later:

    SELECT created_at, event_type, reason FROM moderation_events
    WHERE user_id = <id> ORDER BY created_at DESC;

**Help desk**
- `/start` with no topic shows a menu: *Nu primesc SMS-ul* (answers with the Google sign-in
  workaround), *Revendicare profil* (same as the `revendicare` topic below, support is
  alerted), *Cum pot posta în grup* (the rules + her invite link), *Telegram pe profilul meu*
  (where to add it on the site), *Vorbește cu un om*. Only what the buttons cannot answer
  reaches a person.
- The site's help links open the private chat as `https://t.me/approape_guard_bot?start=<topic>`:
  - `ajutor` — general help;
  - `cont` — could not create an account;
  - `revendicare` — wants to claim a profile but the SMS does not arrive.
  The bot tells the person what to send. For `cont` and `revendicare` it also alerts
  `SUPPORT_CHAT_ID`, and a reply to that alert reaches the person.
- Anything written to the bot privately reaches `SUPPORT_CHAT_ID`. An admin's **reply**
  to that message goes back to the person.

**Scheduled posts** (2–3 a day, between 10:00 and 01:00 Bucharest time; quiet 01:00–10:00)
- Profiles and questions alternate. For a profile, the bot tries in this order: a profile
  created in the last 30 days, last week's most viewed profile (`/api/top-weekly`), or a
  recommended one (claimed by its owner, recently updated). It posts the main photo, the
  name, the city and a link tagged `utm_source=telegram`.
- Only profiles the site lists in its sitemaps are posted, so the site's visibility rules
  apply as they are; hidden, inactive and away profiles are skipped. A profile is not
  repeated within 30 days, a question not within 14.
- Questions are open questions or anonymous polls (`QUESTIONS` in `posts.py`).
- The bot never posts if nobody has written in the group since its previous post.
- Driven by `/tick`, see below. Every post is in the `bot_posts` table.

## Commands

| Command | Where | Who |
|---|---|---|
| `/start` | private | anyone — help menu (`?start=invite` → rules + invite link) |
| `/invite`, `/status` | private | anyone — rules + invite link and how many she brought |
| `/invite`, `/status` | group | button to the private chat |
| `/stats` | group | admins |
| `/whitelist`, `/unwhitelist` | group, as a reply to the person or with her id | admins |
| `/info` | group (reply or id) or private (id) | admins — status in the group, invites brought, ads used in 24h, violations |
| `/unlock` | group (reply or id) or private (id) | admins — lifts a mute; unlike `/whitelist`, every other rule still applies |
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

For scheduled posts:

    TICK_SECRET=at least 16 random characters, also set in the cron job

Optional (defaults shown):

    DUPLICATE_COOLDOWN_HOURS=6
    SIMILARITY_THRESHOLD=0.92
    VIOLATION_WINDOW_HOURS=48
    MUTE_MINUTES=60
    PORT=10000
    WEBHOOK_URL=https://...      (not needed on Render: RENDER_EXTERNAL_URL is used)

## Render Free

`GET /` returns `"commit"`: the first 7 characters of `RENDER_GIT_COMMIT`, which Render
sets on every deploy. That is how to tell whether a merge is live.

The service sleeps after ~15 minutes without traffic and wakes on the next update from
Telegram (first response ~30–60s; Telegram retries in the meantime). The webhook is set on
every start and **never deleted** on shutdown, otherwise the bot would never wake up.
All state lives in Postgres, so restarts and redeploys lose nothing. Note: a Supabase
project with no activity for 7 days gets paused; an active group keeps it awake.

### Scheduled posts: the cron job

Render Free cannot wake itself, so an external cron calls the bot. On
[cron-job.org](https://cron-job.org) (free), create a job:

- URL: `https://<service>.onrender.com/tick`, every **10 minutes**
- Advanced → Headers: `X-Tick-Secret: <TICK_SECRET>`
- Timeout: 30s (the first call after a sleep can take that long)

Each call keeps the service awake, and the bot decides whether to post. An always-on
service uses ~744 of the 750 free instance hours a month, so it only fits if it is the
only free service on the Render account. Without `TICK_SECRET`, `/tick` answers 403 and
nothing is posted.

## Tests

    pip install -r requirements-dev.txt
    docker run -d --rm --name tgbot-pg -e POSTGRES_PASSWORD=test -p 55432:5432 postgres:16-alpine
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/postgres pytest

Without `TEST_DATABASE_URL`, only the pure moderation tests run.
