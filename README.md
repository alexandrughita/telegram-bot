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
- A message containing a link to **approape.ro** (or a subdomain, including a link hidden
  behind text) is exempt from every rule below.
- **Illegal content** (minors, pedophilia, zoophilia, gore, snuff: `ILLEGAL_RE` in
  `moderation.py`) bans the sender outright, whitelist or not, deletes all her messages and
  alerts `SUPPORT_CHAT_ID` with the text. An **age under 18** ("16 ani", "sub 18") may be
  legal ("nu accept sub 18 ani"), so it is deleted and sent to support with `/ban <id>`.
  Text only: images are not inspected, use `/ban` for those.
- **An ad by its content** counts as an ad even the first time, with no link: selling words
  (`AD_KEYWORDS` in `moderation.py`: show web, sexting, dick rating, canal/grup privat,
  Snapchat, OnlyFans, "mesaj în privat", "scrie-mi aici"…, matched without diacritics,
  spaces or fancy fonts), or 5+ custom emoji in one message.
- **Ads: unverified members cannot post any ads.** Every detected ad from an unverified
  member is deleted immediately, including the first one, with this notice:
  `🚫 Reclamele sunt interzise utilizatorilor neverificați.` and
  `📹 Trimite videoclipul de verificare în privat la @approape_ro.` The video is sent to
  @approape_ro in a private chat; the bot does not tell the sender what the video must
  contain. After an admin runs `/verifica`, she may post up to 3 ads per 24 hours. A
  detected ad is a message with a link outside approape.ro, the same text (≥ 15 characters
  after normalising) posted again within 24 hours, a reworded copy (≥ 25 characters,
  ~75% alike; "fancy font" letters count as plain ones), an ad detected by its content, or
  a qualifying forward of recent text posted in the group. Verified members are blocked on
  their fourth ad in the 24-hour window. Rejected ads are not violations. A week after a
  verified member hits the daily limit, she gets a private reminder when the limit has lifted.
- **Tagged "Reclamă neverif.": every message is an ad.** Once she carries that tag and is
  not verified, anything she posts is deleted with the same notice until an admin runs `/verifica`.
  Links to approape.ro are still allowed.
- **Selling phrases from unverified members are ads.** Whole words, diacritics and fancy fonts
  ignored: `web`/`webb`, `băiat/domn serios`, `vând content/clipuri/poze`, `ofer (un) clip(uri)`, `ofer poze`,
  `grup(uri) privat(e)`, `sexting`, `porno`, `pachete` ("libera la webb", "caut un baiat serios in privat").
  Clients are unverified too, so words they use (`liberă`, `pv`, `în privat`) are not on the list.
  Verified members may use them.
- Other repeats within 6 hours are violations: the same photo/clip, the same link
  (including links hidden behind text) or the same Romanian mobile number.
- Stickers: at most 2 static and 2 animated/video per 24 hours, counted separately (a burst
  delivered all at once still counts). The next one is deleted with a note asking for a
  real message; it is not a violation.
- GIFs are not limited.
- Escalation within 48h: 1st violation = delete only; 2nd and 3rd = delete + warning;
  4th onwards = delete + mute for 60 minutes.
- Messages posted "as a channel" are deleted; anonymous admins and posts from the linked
  channel are left alone.

**Member tags** (Telegram's tag next to her name; the bot needs the *manage tags* right)
- Whoever posts an ad (caught by the bot or marked with `/reclama`) is tagged
  **Reclamă neverif.** (Telegram allows 16 characters); `/verifica` changes it to
  **Verificată**, `/neverifica` back.
  Admins cannot be tagged (Telegram tags regular members only). A refused tag is only logged.

**Admin overrides**
- `/reclama`, as a reply to a message the rules missed: it counts as her ad, exactly as if
  the bot had caught it. If she had no ads left it is deleted with the limit note.
  The bot learns from it for good (`learned_ads`): a similar text (~75% alike) from anyone
  is an ad from then on. Words after the command, comma-separated, become keywords
  (`ad_keywords`, at least 4 letters): `/reclama video call, cam show`, also without a reply.
- `/nureclama` undoes it: as a reply, the learned texts like that message are forgotten and
  it no longer counts as her ad; `/nureclama video call` removes keywords; alone, it lists
  what the bot learned.
- `/mute`, or just `mute`, as a reply to her message (or `/mute <id>`): she is read-only with no
  end date. Admins cannot be muted. An **unverified member who posts an ad** is muted the same
  way, right after her ad is deleted.
- `unmute @username` (or `unmute <id>`), written **to the bot in private** by an admin, lifts
  the mute (same as `/unlock`). The @username is found only for members the bot has seen in the
  group. `/unlock` and `/verifica` also lift it. If she is still unverified, her next ad mutes
  her again. From anyone who is not an admin the message goes to the help desk as usual.
- `/ban`, as a reply or with her id: she is banned for good and her messages are deleted
  (Telegram's `revoke_messages`, plus the last 24h the bot has on record). Needs the
  *Ban users* right; a refusal is explained to the admin.

If Telegram refuses a deletion (usually: the bot lost *Delete messages*), it is logged as
`delete_failed` with Telegram's reason, shown as **NEȘTERS** in `/info`, and the first
refusal since start is sent to `SUPPORT_CHAT_ID`.

Every action (delete, warn, mute, unlock, ban) is logged in `moderation_events` with its reason
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
- Every ad the bot deletes is copied to `SUPPORT_CHAT_ID` first, with her name, id and the
  reason. A **reply** to it reaches her privately, for when it was not an ad. Telegram lets
  the bot write to her only if she has started the bot; otherwise tap her name and write yourself.

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
| `/stats` | group | admins — answered privately, the command is deleted |
| `/whitelist`, `/unwhitelist` | group, as a reply to the person or with her id | admins |
| `/info` | group (reply or id) or private (id) | admins — status in the group, invites brought, ads used in 24h, violations |
| `/unlock` | group (reply or id) or private (id) | admins — lifts a mute; unlike `/whitelist`, every other rule still applies |
| `/reclama [cuvinte, …]` | group, as a reply (or just the words) | admins — counts the message as her ad, learns its text; words become keywords |
| `/nureclama [cuvinte, …]` | group (reply or words) or private (words) | admins — forgets learned texts / keywords; alone, lists them |
| `/verifica`, `/neverifica` | group (reply, or a list of @usernames / ids) or private (list) | admins — `/verifica` allows up to 3 ads per 24 hours and sets the **Verificată** tag; `/neverifica` blocks all ads until verification again. A @username works only for members the bot has seen in the group |
| `/ban` | group (reply or id) or private (id) | admins — bans her and deletes her messages |
| `/chatid` | anywhere | group admins / anyone in private |

## Setup

1. **BotFather:** create the bot and keep the token.
2. **Group:** it must be a supergroup. Add the bot as an administrator with: *Delete messages*,
   *Ban/restrict users*, *Invite users via link*, *manage tags*.
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
