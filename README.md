# Telegram Group Bot

Telegram moderation/growth bot for a group.

## Current features

- Personal invite links
- Invite tracking
- 3 valid invites -> posting unlock
- New members are restricted until unlocked
- Duplicate / near-duplicate message detection
- Duplicate image detection using Telegram file_unique_id
- Same URL cooldown
- GIF spam protection across consecutive messages
- Warning counter and automatic mute
- Admin bypass
- Render Web Service + Telegram webhook

## Important Telegram setup

The bot must be an administrator in the group with at least:

- Delete messages
- Restrict members
- Invite users via link

For `/setupadmin`, run the command from an actual Telegram group administrator account.

## Render environment variables

Required:

BOT_TOKEN=your BotFather token
WEBHOOK_URL=https://your-service.onrender.com
WEBHOOK_SECRET=some-long-random-secret

Optional:

INVITES_REQUIRED=3
DUPLICATE_COOLDOWN_HOURS=6
GIF_WINDOW_SECONDS=60
GIF_MAX_IN_WINDOW=1
SIMILARITY_THRESHOLD=0.92
AUTO_MUTE_AFTER=3
AUTO_MUTE_MINUTES=60
PORT=10000

## Important note about GIFs

Telegram represents a GIF as an Animation. One Telegram message cannot contain multiple animations, so the bot additionally limits GIF messages from the same user to one in the configured time window.

## Persistence

This first version uses SQLite (`bot.db`). Render Free does not provide persistent disks, so database persistence will be lost if the service is recreated/redeployed. For production, move the DB to an external PostgreSQL database or another persistent datastore.
