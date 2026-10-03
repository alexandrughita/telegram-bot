import os

# bot.py reads its configuration at import time.
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("GROUP_CHAT_ID", "-100777")
os.environ.setdefault("SUPPORT_CHAT_ID", "-100999")
