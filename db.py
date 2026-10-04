"""Postgres storage (Supabase in production)."""
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from moderation import Fingerprint

SCHEMA = """
CREATE TABLE IF NOT EXISTS members (
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    username TEXT,
    first_name TEXT,
    unlocked BOOLEAN NOT NULL DEFAULT FALSE,
    legacy BOOLEAN NOT NULL DEFAULT FALSE,
    first_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS invite_links (
    chat_id BIGINT NOT NULL, inviter_id BIGINT NOT NULL, invite_link TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (chat_id, inviter_id)
);
CREATE TABLE IF NOT EXISTS invites (
    chat_id BIGINT NOT NULL, invited_user_id BIGINT NOT NULL, inviter_id BIGINT NOT NULL,
    joined_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (chat_id, invited_user_id)
);
CREATE INDEX IF NOT EXISTS idx_invites_inviter ON invites(chat_id, inviter_id);
CREATE TABLE IF NOT EXISTS messages (
    id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL, text TEXT NOT NULL DEFAULT '', urls TEXT[] NOT NULL DEFAULT '{}',
    phones TEXT[] NOT NULL DEFAULT '{}', media TEXT[] NOT NULL DEFAULT '{}',
    is_gif BOOLEAN NOT NULL DEFAULT FALSE, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_user_time ON messages(chat_id, user_id, created_at);
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_sticker BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_static_sticker BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE messages ADD COLUMN IF NOT EXISTS is_ad BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_messages_ads ON messages(chat_id, user_id, created_at) WHERE is_ad;
CREATE TABLE IF NOT EXISTS violations (
    id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
    reason TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_violations_user_time ON violations(chat_id, user_id, created_at);
CREATE TABLE IF NOT EXISTS moderation_events (
    id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, user_id BIGINT NOT NULL,
    message_id BIGINT, event_type TEXT NOT NULL, reason TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_moderation_events_user ON moderation_events(chat_id, user_id, created_at);
CREATE TABLE IF NOT EXISTS whitelist (
    chat_id BIGINT NOT NULL, user_id BIGINT NOT NULL, added_by BIGINT NOT NULL,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY (chat_id, user_id)
);
CREATE TABLE IF NOT EXISTS bot_posts (
    id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, kind TEXT NOT NULL,
    ref TEXT NOT NULL, posted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_bot_posts_time ON bot_posts(chat_id, posted_at);
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY, at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS support_threads (
    support_message_id BIGINT PRIMARY KEY, user_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

class Store:
    def __init__(self, dsn):
        self.pool = AsyncConnectionPool(
            dsn, min_size=1, max_size=5, open=False,
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row},
        )
    async def open(self):
        await self.pool.open()
        await self._execute(SCHEMA)
    async def close(self): await self.pool.close()
    async def _execute(self, sql, params=None):
        async with self.pool.connection() as conn: return await conn.execute(sql, params)
    async def _one(self, sql, params=None):
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params); return await cur.fetchone()
    async def _all(self, sql, params=None):
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params); return await cur.fetchall()

    async def get_member(self, chat_id, user_id):
        return await self._one("SELECT * FROM members WHERE chat_id=%s AND user_id=%s", (chat_id,user_id))
    async def add_member(self, chat_id, user, unlocked=False, legacy=False):
        await self._execute("""INSERT INTO members(chat_id,user_id,username,first_name,unlocked,legacy)
            VALUES(%s,%s,%s,%s,%s,%s)
            ON CONFLICT(chat_id,user_id) DO UPDATE SET username=EXCLUDED.username,first_name=EXCLUDED.first_name""",
            (chat_id,user.id,user.username,user.first_name,unlocked,legacy))
        return await self.get_member(chat_id,user.id)
    async def set_unlocked(self, chat_id,user_id):
        await self._execute("UPDATE members SET unlocked=TRUE WHERE chat_id=%s AND user_id=%s",(chat_id,user_id))

    async def get_invite_link(self,chat_id,inviter_id):
        r=await self._one("SELECT invite_link FROM invite_links WHERE chat_id=%s AND inviter_id=%s",(chat_id,inviter_id))
        return r["invite_link"] if r else None
    async def save_invite_link(self,chat_id,inviter_id,link):
        await self._execute("""INSERT INTO invite_links(chat_id,inviter_id,invite_link) VALUES(%s,%s,%s)
            ON CONFLICT(chat_id,inviter_id) DO UPDATE SET invite_link=EXCLUDED.invite_link""",(chat_id,inviter_id,link))
    async def inviter_for_link(self,chat_id,link):
        r=await self._one("SELECT inviter_id FROM invite_links WHERE chat_id=%s AND invite_link=%s",(chat_id,link))
        return r["inviter_id"] if r else None
    async def record_invite(self,chat_id,invited_user_id,inviter_id):
        cur=await self._execute("""INSERT INTO invites(chat_id,invited_user_id,inviter_id) VALUES(%s,%s,%s)
            ON CONFLICT DO NOTHING""",(chat_id,invited_user_id,inviter_id))
        return cur.rowcount==1
    async def invite_count(self,chat_id,inviter_id):
        r=await self._one("SELECT COUNT(*) AS n FROM invites WHERE chat_id=%s AND inviter_id=%s",(chat_id,inviter_id))
        return r["n"]

    async def recent_fingerprints(self,chat_id,user_id,hours):
        rows=await self._all("""SELECT text,urls,phones,media FROM messages
            WHERE chat_id=%s AND user_id=%s AND created_at >= now() - %s * interval '1 hour'""",(chat_id,user_id,hours))
        return [Fingerprint(r["text"],set(r["urls"]),set(r["phones"]),set(r["media"])) for r in rows]
    async def recent_gif_count(self,chat_id,user_id,seconds):
        r=await self._one("""SELECT COUNT(*) AS n FROM messages WHERE chat_id=%s AND user_id=%s AND is_gif
            AND created_at >= now() - %s * interval '1 second'""",(chat_id,user_id,seconds))
        return r["n"]
    async def recent_sticker_count(self,chat_id,user_id,hours,static=False):
        if static is None:
            condition = "(is_sticker OR is_static_sticker)"
        else:
            condition = "is_static_sticker" if static else "is_sticker"
        r=await self._one(f"""SELECT COUNT(*) AS n FROM messages WHERE chat_id=%s AND user_id=%s AND {condition}
            AND created_at >= now() - %s * interval '1 hour'""",(chat_id,user_id,hours))
        return r["n"]
    async def recent_ad_count(self,chat_id,user_id,hours):
        r=await self._one("""SELECT COUNT(*) AS n FROM messages WHERE chat_id=%s AND user_id=%s AND is_ad
            AND created_at >= now() - %s * interval '1 hour'""",(chat_id,user_id,hours))
        return r["n"]
    async def oldest_ad(self,chat_id,user_id,hours):
        return await self._one("""SELECT created_at FROM messages WHERE chat_id=%s AND user_id=%s AND is_ad
            AND created_at >= now() - %s * interval '1 hour' ORDER BY created_at ASC LIMIT 1""",(chat_id,user_id,hours))
    async def ad_limit_users(self,chat_id,days):
        return await self._all("""SELECT user_id FROM moderation_events
            WHERE chat_id=%s AND event_type='delete' AND reason LIKE 'reclame: limita 2/24h%%'
            AND created_at >= now() - %s * interval '1 day'
            GROUP BY user_id""",(chat_id,days))
    async def save_message(self,chat_id,user_id,message_id,fp,is_gif,is_sticker=False,is_static_sticker=False,is_ad=False):
        await self._execute("""INSERT INTO messages(chat_id,user_id,message_id,text,urls,phones,media,is_gif,is_sticker,is_static_sticker,is_ad)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (chat_id,user_id,message_id,fp.text,sorted(fp.urls),sorted(fp.phones),sorted(fp.media),
             is_gif,is_sticker,is_static_sticker,is_ad))

    async def add_violation(self,chat_id,user_id,reason,window_hours):
        await self._execute("INSERT INTO violations(chat_id,user_id,reason) VALUES(%s,%s,%s)",(chat_id,user_id,reason))
        return await self.violation_count(chat_id,user_id,window_hours)
    async def violation_count(self,chat_id,user_id,window_hours):
        r=await self._one("""SELECT COUNT(*) AS n FROM violations WHERE chat_id=%s AND user_id=%s
            AND created_at >= now() - %s * interval '1 hour'""",(chat_id,user_id,window_hours)); return r["n"]
    async def recent_events(self,chat_id,user_id,limit):
        return await self._all("""SELECT event_type,reason,created_at FROM moderation_events
            WHERE chat_id=%s AND user_id=%s ORDER BY created_at DESC,id DESC LIMIT %s""",(chat_id,user_id,limit))
    async def log_event(self,chat_id,user_id,event_type,reason=None,message_id=None):
        await self._execute("""INSERT INTO moderation_events(chat_id,user_id,message_id,event_type,reason)
            VALUES(%s,%s,%s,%s,%s)""",(chat_id,user_id,message_id,event_type,reason))

    async def save_support_thread(self,support_message_id,user_id):
        await self._execute("INSERT INTO support_threads(support_message_id,user_id) VALUES(%s,%s) ON CONFLICT DO NOTHING",(support_message_id,user_id))
    async def support_user_for(self,support_message_id):
        r=await self._one("SELECT user_id FROM support_threads WHERE support_message_id=%s",(support_message_id,)); return r["user_id"] if r else None
    async def is_whitelisted(self,chat_id,user_id):
        r=await self._one("SELECT 1 AS x FROM whitelist WHERE chat_id=%s AND user_id=%s",(chat_id,user_id)); return r is not None
    async def add_whitelist(self,chat_id,user_id,added_by):
        await self._execute("INSERT INTO whitelist(chat_id,user_id,added_by) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",(chat_id,user_id,added_by))
    async def remove_whitelist(self,chat_id,user_id):
        cur=await self._execute("DELETE FROM whitelist WHERE chat_id=%s AND user_id=%s",(chat_id,user_id)); return cur.rowcount==1
    async def get_time(self,key):
        r=await self._one("SELECT at FROM bot_state WHERE key=%s",(key,)); return r["at"] if r else None
    async def set_time(self,key,at):
        await self._execute("INSERT INTO bot_state(key,at) VALUES(%s,%s) ON CONFLICT(key) DO UPDATE SET at=EXCLUDED.at",(key,at))
    async def last_post(self,chat_id):
        return await self._one("SELECT kind,posted_at FROM bot_posts WHERE chat_id=%s ORDER BY posted_at DESC LIMIT 1",(chat_id,))
    async def recent_post_refs(self,chat_id,days,question=False):
        rows=await self._all("""SELECT DISTINCT ref FROM bot_posts WHERE chat_id=%s AND (kind='question')=%s
            AND posted_at >= now() - %s * interval '1 day'""",(chat_id,question,days)); return {r["ref"] for r in rows}
    async def record_post(self,chat_id,kind,ref,posted_at):
        await self._execute("INSERT INTO bot_posts(chat_id,kind,ref,posted_at) VALUES(%s,%s,%s,%s)",(chat_id,kind,ref,posted_at))
    async def cleanup(self,message_hours,violation_days=30):
        await self._execute("DELETE FROM messages WHERE created_at < now() - %s * interval '1 hour'",(message_hours,))
        await self._execute("DELETE FROM violations WHERE created_at < now() - %s * interval '1 day'",(violation_days,))
    async def stats(self,chat_id):
        return await self._one("""SELECT
          (SELECT COUNT(*) FROM members WHERE chat_id=%(c)s) AS members,
          (SELECT COUNT(*) FROM members WHERE chat_id=%(c)s AND unlocked) AS unlocked,
          (SELECT COUNT(*) FROM invites WHERE chat_id=%(c)s) AS invites,
          (SELECT COUNT(*) FROM violations WHERE chat_id=%(c)s AND created_at >= now()-interval '7 days') AS violations_7d""",{"c":chat_id})
