"""Handler tests against a real Postgres. Telegram is faked.

    TEST_DATABASE_URL=postgresql://... pytest
"""
import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram import Update

import bot
from db import Store

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")

G = bot.GROUP_CHAT_ID
S = bot.SUPPORT_CHAT_ID
ADMIN_ID = 1
BOT_USER = {"id": 999, "is_bot": True, "first_name": "Bot", "username": "approape_bot"}


def user(uid, is_bot=False):
    return {"id": uid, "is_bot": is_bot, "first_name": f"U{uid}"}


class FakeBot:
    def __init__(self):
        self.username = "approape_bot"
        self.links = 0
        self.restrict_chat_member = AsyncMock()
        self.delete_message = AsyncMock()
        self.send_message = AsyncMock(return_value=SimpleNamespace(message_id=500))
        self.copy_message = AsyncMock(side_effect=lambda *a, **k: SimpleNamespace(message_id=501))
        self.get_chat = AsyncMock(return_value=SimpleNamespace(permissions=None))
        self.get_chat_administrators = AsyncMock(
            return_value=[SimpleNamespace(user=SimpleNamespace(id=ADMIN_ID))])
        self.get_chat_member = AsyncMock(return_value=SimpleNamespace(status="member"))

    async def create_chat_invite_link(self, chat_id, name):
        self.links += 1
        return SimpleNamespace(invite_link=f"https://t.me/+link{self.links}")

    def posting_unlocked(self, uid):
        return any(c.args[1] == uid and c.args[2] is bot.FALLBACK_POSTING
                   for c in self.restrict_chat_member.call_args_list)


@pytest.fixture
def run():
    loop = asyncio.new_event_loop()
    yield loop.run_until_complete
    loop.close()


@pytest.fixture
def env(run):
    store = Store(DSN)
    run(store.open())
    run(store._execute("TRUNCATE members, invite_links, invites, messages, violations, support_threads, moderation_events"))
    bot.store = store
    bot._admin_cache.clear()
    bot._permissions_cache.clear()
    bot._support_ack_at.clear()
    fake = FakeBot()
    yield SimpleNamespace(bot=fake, store=store, ctx=SimpleNamespace(bot=fake, args=[]))
    run(asyncio.sleep(0))
    run(store.close())


def join(env, run, uid, link=None, old="left", new="member", is_bot=False):
    data = {
        "update_id": 1,
        "chat_member": {
            "chat": {"id": G, "type": "supergroup", "title": "g"},
            "from": user(uid), "date": 0,
            "old_chat_member": {"status": old, "user": user(uid, is_bot)},
            "new_chat_member": {"status": new, "user": user(uid, is_bot)},
        },
    }
    if link:
        data["chat_member"]["invite_link"] = {
            "invite_link": link, "creator": BOT_USER, "creates_join_request": False,
            "is_primary": False, "is_revoked": False}
    run(bot.on_chat_member(Update.de_json(data, None), env.ctx))


def message(env, run, uid, text=None, chat=G, mid=10, **extra):
    msg = {"message_id": mid, "date": 0, "from": user(uid),
           "chat": {"id": chat, "type": "supergroup" if chat == G else "private"}, **extra}
    if text is not None:
        msg["text"] = text
    upd = Update.de_json({"update_id": 2, "message": msg}, None)
    handler = bot.on_group_message if chat == G else bot.on_private_message
    run(handler(upd, env.ctx))
    return upd


def private_command(env, run, uid, text):
    upd = Update.de_json({"update_id": 3, "message": {
        "message_id": 1, "date": 0, "from": user(uid), "text": text,
        "chat": {"id": uid, "type": "private"}}}, env.bot)
    return upd


def test_three_joins_through_her_link_unlock_her(env, run):
    join(env, run, 50)  # she joins herself first, so she is a tracked, locked member
    upd = private_command(env, run, 50, "/invite")
    run(bot.invite_status(upd, env.ctx))
    link = run(env.store.get_invite_link(G, 50))
    assert link and link in env.bot.send_message.call_args.kwargs["text"]

    for invited in (61, 62):
        join(env, run, invited, link=link)
    assert not env.bot.posting_unlocked(50)
    join(env, run, 63, link=link)
    assert env.bot.posting_unlocked(50)
    assert run(env.store.get_member(G, 50))["unlocked"]
    assert events(env, run, 50) == ["unlock"]


def events(env, run, uid):
    rows = run(env.store._all(
        "SELECT event_type FROM moderation_events WHERE user_id=%s ORDER BY id", (uid,)))
    return [r["event_type"] for r in rows]


def test_one_link_per_person(env, run):
    for _ in range(2):
        run(bot.invite_status(private_command(env, run, 50, "/invite"), env.ctx))
    assert env.bot.links == 1


def test_rejoin_self_invite_and_bots_earn_nothing(env, run):
    join(env, run, 50)
    run(bot.invite_status(private_command(env, run, 50, "/invite"), env.ctx))
    link = run(env.store.get_invite_link(G, 50))
    join(env, run, 50, link=link)               # self, as a rejoin
    join(env, run, 70, link=link, is_bot=True)  # a bot
    join(env, run, 71)                          # 71 joins on their own...
    join(env, run, 71, link=link)               # ...then leaves and comes back through her link
    assert run(env.store.invite_count(G, 50)) == 0


def test_new_member_is_restricted_and_her_posts_deleted(env, run):
    join(env, run, 80)
    assert env.bot.restrict_chat_member.call_args.args[1:3] == (80, bot.READ_ONLY)
    message(env, run, 80, "salut")
    env.bot.delete_message.assert_awaited_with(G, 10)
    assert events(env, run, 80) == ["delete"]


def test_member_from_before_the_bot_keeps_posting(env, run):
    message(env, run, 90, "Bună tuturor, sunt aici de mult timp")
    env.bot.delete_message.assert_not_awaited()
    assert run(env.store.get_member(G, 90))["legacy"]


def test_unlocked_member_who_rejoins_can_post_again(env, run):
    message(env, run, 90, "prima postare")  # legacy -> unlocked
    join(env, run, 90)
    assert env.bot.posting_unlocked(90)


def test_admin_is_never_moderated_even_untracked(env, run):
    for i in range(5):
        message(env, run, ADMIN_ID, "Același anunț de la admin, sună 0722123456", mid=i)
    env.bot.delete_message.assert_not_awaited()


def test_repeated_ad_escalates_delete_warn_warn_mute(env, run):
    message(env, run, 90, "Anunț: sună 0722123456", mid=1)
    env.bot.send_message.reset_mock()
    for mid in range(2, 6):
        message(env, run, 90, f"Variantă {mid}, tot 0722 123 456", mid=mid)
    assert env.bot.delete_message.await_count == 4
    notices = [c.args[1] for c in env.bot.send_message.call_args_list]
    assert len(notices) == 3
    assert "Avertisment 1/2" in notices[0] and "Avertisment 2/2" in notices[1]
    assert "mute" in notices[2]
    mute = env.bot.restrict_chat_member.call_args
    assert mute.args[1] == 90 and mute.kwargs.get("until_date")
    assert events(env, run, 90) == ["delete", "warn", "warn", "mute"]


def test_second_gif_within_a_minute_is_removed(env, run):
    gif = {"file_id": "a", "file_unique_id": "gif1", "width": 1, "height": 1, "duration": 1}
    message(env, run, 90, mid=1, animation=gif, document={"file_id": "a", "file_unique_id": "gif1"})
    gif2 = {**gif, "file_unique_id": "gif2"}
    message(env, run, 90, mid=2, animation=gif2, document={"file_id": "b", "file_unique_id": "gif2"})
    env.bot.delete_message.assert_awaited_once_with(G, 2)


def test_hidden_link_counts_as_a_link(env, run):
    message(env, run, 90, "vezi aici", mid=1,
            entities=[{"type": "text_link", "offset": 0, "length": 4, "url": "https://x.ro/a"}])
    message(env, run, 90, "detalii", mid=2,
            entities=[{"type": "text_link", "offset": 0, "length": 7, "url": "https://www.x.ro/a/"}])
    env.bot.delete_message.assert_awaited_once_with(G, 2)


def test_help_message_reaches_support_and_reply_comes_back(env, run):
    upd = Update.de_json({"update_id": 4, "message": {
        "message_id": 7, "date": 0, "from": user(42), "text": "SMS-ul nu vine",
        "chat": {"id": 42, "type": "private"}}}, env.bot)
    run(bot.on_private_message(upd, env.ctx))
    env.bot.copy_message.assert_awaited_with(S, 42, 7)

    reply = Update.de_json({"update_id": 5, "message": {
        "message_id": 8, "date": 0, "from": user(ADMIN_ID), "text": "Te ajutăm",
        "chat": {"id": S, "type": "supergroup"},
        "reply_to_message": {"message_id": 501, "date": 0, "chat": {"id": S, "type": "supergroup"}}}}, None)
    run(bot.on_support_reply(reply, env.ctx))
    env.bot.copy_message.assert_awaited_with(42, S, 8)
