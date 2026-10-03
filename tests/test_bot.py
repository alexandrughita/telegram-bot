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
    bot._moderation_lock = asyncio.Lock()  # each test runs on its own event loop
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


def posting_member(env, run, uid):
    """A member who has already earned the right to post."""
    run(env.store.add_member(G, SimpleNamespace(id=uid, username=None, first_name=f"U{uid}"), unlocked=True))


def gif_update(uid, mid, unique_id):
    gif = {"file_id": unique_id, "file_unique_id": unique_id, "width": 1, "height": 1, "duration": 1}
    return Update.de_json({"update_id": mid, "message": {
        "message_id": mid, "date": 0, "from": user(uid), "chat": {"id": G, "type": "supergroup"},
        "animation": gif, "document": {"file_id": unique_id, "file_unique_id": unique_id}}}, None)


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


def test_member_from_before_the_bot_is_locked_until_three_invites(env, run):
    message(env, run, 90, "Bună tuturor, sunt aici de mult timp")
    env.bot.delete_message.assert_awaited_with(G, 10)
    assert env.bot.restrict_chat_member.call_args.args[1:3] == (90, bot.READ_ONLY)
    assert "3 membri" in env.bot.send_message.call_args.args[1]
    member = run(env.store.get_member(G, 90))
    assert member["legacy"] and not member["unlocked"]

    run(bot.invite_status(private_command(env, run, 90, "/invite"), env.ctx))
    link = run(env.store.get_invite_link(G, 90))
    for invited in (91, 92, 93):
        join(env, run, invited, link=link)
    assert env.bot.posting_unlocked(90)


def test_member_from_before_the_bot_asking_for_her_link_is_still_locked(env, run):
    run(bot.invite_status(private_command(env, run, 90, "/invite"), env.ctx))
    assert not run(env.store.get_member(G, 90))["unlocked"]
    assert "Mai ai nevoie de 3" in env.bot.send_message.call_args.kwargs["text"]


def test_unlocked_member_who_rejoins_can_post_again(env, run):
    posting_member(env, run, 90)
    join(env, run, 90)
    assert env.bot.posting_unlocked(90)


def test_admin_is_never_moderated_even_untracked(env, run):
    for i in range(5):
        message(env, run, ADMIN_ID, "Același anunț de la admin, sună 0722123456", mid=i)
    env.bot.delete_message.assert_not_awaited()


def test_repeated_ad_escalates_delete_warn_warn_mute(env, run):
    posting_member(env, run, 90)
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
    posting_member(env, run, 90)
    gif = {"file_id": "a", "file_unique_id": "gif1", "width": 1, "height": 1, "duration": 1}
    message(env, run, 90, mid=1, animation=gif, document={"file_id": "a", "file_unique_id": "gif1"})
    gif2 = {**gif, "file_unique_id": "gif2"}
    message(env, run, 90, mid=2, animation=gif2, document={"file_id": "b", "file_unique_id": "gif2"})
    env.bot.delete_message.assert_awaited_once_with(G, 2)


def test_one_animated_sticker_message_a_day(env, run):
    posting_member(env, run, 90)
    sticker = {"file_id": "s", "file_unique_id": "st1", "width": 512, "height": 512,
               "type": "regular", "is_animated": True, "is_video": False}
    message(env, run, 90, mid=1, sticker=sticker)
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '23 hours'"))
    message(env, run, 90, mid=2, sticker={**sticker, "file_unique_id": "st2", "is_animated": False, "is_video": True})
    env.bot.delete_message.assert_awaited_once_with(G, 2)
    notice = env.bot.send_message.call_args.args[1]
    assert "un mesaj cu stickere animate pe zi" in notice and "approape.ro" in notice
    assert events(env, run, 90) == ["delete"]  # not a violation: no warning, no mute
    assert run(env.store._one("SELECT COUNT(*) AS n FROM violations"))["n"] == 0


def test_animated_sticker_allowed_again_after_a_day(env, run):
    posting_member(env, run, 90)
    sticker = {"file_id": "s", "file_unique_id": "st1", "width": 512, "height": 512,
               "type": "regular", "is_animated": True, "is_video": False}
    message(env, run, 90, mid=1, sticker=sticker)
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '25 hours'"))
    message(env, run, 90, mid=2, sticker={**sticker, "file_unique_id": "st2"})
    env.bot.delete_message.assert_not_awaited()


def test_static_stickers_are_not_limited(env, run):
    posting_member(env, run, 90)
    sticker = {"file_id": "s", "file_unique_id": "st1", "width": 512, "height": 512,
               "type": "regular", "is_animated": False, "is_video": False}
    message(env, run, 90, mid=1, sticker=sticker)
    message(env, run, 90, mid=2, sticker={**sticker, "file_unique_id": "st2"})
    env.bot.delete_message.assert_not_awaited()


def test_a_message_with_an_approape_link_is_exempt_from_every_rule(env, run):
    posting_member(env, run, 90)
    for mid in range(1, 4):
        message(env, run, 90, "Profilul meu: https://www.approape.ro/escorte/ana sună 0722123456", mid=mid)
    message(env, run, 90, "detalii", mid=4,
            entities=[{"type": "text_link", "offset": 0, "length": 7, "url": "https://approape.ro/escorte/ana"}])
    # Telegram marks a bare "approape.ro/..." as a url entity itself.
    message(env, run, 91, "Nouă aici, vezi approape.ro/creatoare/ana", mid=5,  # locked, pre-bot
            entities=[{"type": "url", "offset": 16, "length": 25}])
    env.bot.delete_message.assert_not_awaited()


def test_a_lookalike_domain_is_not_approape(env, run):
    posting_member(env, run, 90)
    for mid in (1, 2):
        message(env, run, 90, "Vezi https://approape.ro.example.com/x", mid=mid)
    env.bot.delete_message.assert_awaited_once_with(G, 2)


def test_gifs_arriving_together_are_still_limited(env, run):
    # Telegram delivers a backlog (e.g. when Render wakes up) over parallel
    # webhook requests, so the handlers run concurrently.
    posting_member(env, run, 90)
    updates = [gif_update(90, mid, f"gif{mid}") for mid in range(1, 5)]

    async def burst():
        await asyncio.gather(*(bot.on_group_message(u, env.ctx) for u in updates))
    run(burst())
    assert env.bot.delete_message.await_count == 3


def test_hidden_link_counts_as_a_link(env, run):
    posting_member(env, run, 90)
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
