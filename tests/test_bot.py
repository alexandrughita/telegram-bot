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


def user(uid, is_bot=False, username=None):
    data = {"id": uid, "is_bot": is_bot, "first_name": f"U{uid}"}
    if username:
        data["username"] = username
    return data


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
        self.get_chat_member = AsyncMock(side_effect=lambda chat_id, uid: SimpleNamespace(
            status="member", user=SimpleNamespace(id=uid, username=None, first_name=f"U{uid}", is_bot=False)))
        self.answer_callback_query = AsyncMock()
        self.send_photo = AsyncMock()
        self.send_poll = AsyncMock()

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
def env(run, monkeypatch):
    store = Store(DSN)
    run(store.open())
    run(store._execute("TRUNCATE members, invite_links, invites, messages, violations, support_threads, moderation_events, bot_posts, bot_state, whitelist"))
    bot.store = store
    bot._admin_cache.clear()
    bot._permissions_cache.clear()
    bot._site_handles = None
    site_handles = set()  # usernames published on approape.ro profiles, per test
    monkeypatch.setattr(bot.posts, "fetch_telegram_handles", AsyncMock(side_effect=lambda: set(site_handles)))
    bot._support_ack_at.clear()
    bot._moderation_lock = asyncio.Lock()  # each test runs on its own event loop
    bot._tick_lock = asyncio.Lock()
    fake = FakeBot()
    yield SimpleNamespace(bot=fake, store=store, ctx=SimpleNamespace(bot=fake, args=[]), site_handles=site_handles)
    run(asyncio.sleep(0))
    run(store.close())


def join(env, run, uid, link=None, old="left", new="member", is_bot=False, by=None, username=None):
    """`by`: the member who added her by hand (Telegram reports the adder as `from`)."""
    data = {
        "update_id": 1,
        "chat_member": {
            "chat": {"id": G, "type": "supergroup", "title": "g"},
            "from": user(by or uid), "date": 0,
            "old_chat_member": {"status": old, "user": user(uid, is_bot, username)},
            "new_chat_member": {"status": new, "user": user(uid, is_bot, username)},
        },
    }
    if link:
        data["chat_member"]["invite_link"] = {
            "invite_link": link, "creator": BOT_USER, "creates_join_request": False,
            "is_primary": False, "is_revoked": False}
    run(bot.on_chat_member(Update.de_json(data, None), env.ctx))


def message(env, run, uid, text=None, chat=G, mid=10, username=None, **extra):
    msg = {"message_id": mid, "date": 0, "from": user(uid, username=username),
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


def private_command(env, run, uid, text, username=None):
    upd = Update.de_json({"update_id": 3, "message": {
        "message_id": 1, "date": 0, "from": user(uid, username=username), "text": text,
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
    to_her = [c.args[1] for c in env.bot.send_message.call_args_list if c.args and c.args[0] == 50]
    assert to_her == [f"✅ Ai adus {bot.INVITES_REQUIRED} membri — acum poți posta în grup."]


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


def test_members_added_by_hand_count_for_whoever_added_them(env, run):
    # A member from before the bot, unknown to it, adds three people by hand.
    join(env, run, 120, by=110)
    join(env, run, 121, by=110)
    assert not run(env.store.get_member(G, 110))["unlocked"]
    join(env, run, 122, by=110)
    assert env.bot.posting_unlocked(110)
    assert run(env.store.get_member(G, 110))["legacy"]
    message(env, run, 110, "salut, am adus oameni")
    env.bot.delete_message.assert_not_awaited()


def test_someone_added_by_hand_counts_once_for_whoever_brought_her_first(env, run):
    join(env, run, 120, by=110)
    join(env, run, 120, old="left", by=111)  # leaves, another member adds her again
    assert run(env.store.invite_count(G, 110)) == 1
    assert run(env.store.invite_count(G, 111)) == 0


def test_telegram_on_her_approape_profile_lets_her_post_on_joining(env, run):
    env.site_handles.add("ana_99")
    join(env, run, 130, username="Ana_99")
    assert env.bot.posting_unlocked(130)
    assert not any(c.args[1:3] == (130, bot.READ_ONLY) for c in env.bot.restrict_chat_member.call_args_list)
    env.bot.send_message.assert_not_awaited()  # no welcome asking for invites
    assert events(env, run, 130) == ["unlock"]


def test_member_from_before_the_bot_with_telegram_on_her_profile_keeps_posting(env, run):
    env.site_handles.add("ana_99")
    message(env, run, 131, "Bună tuturor", username="ana_99")
    env.bot.delete_message.assert_not_awaited()
    assert run(env.store.get_member(G, 131))["unlocked"]


def test_locked_member_who_adds_telegram_to_her_profile_is_unlocked_by_start(env, run):
    join(env, run, 132, username="ana_99")
    run(bot.invite_status(private_command(env, run, 132, "/start", username="ana_99"), env.ctx))
    assert "@ana_99) pe profilul tău de pe approape.ro" in env.bot.send_message.call_args.kwargs["text"]
    assert not env.bot.posting_unlocked(132)

    env.site_handles.add("ana_99")
    bot._site_handles = None  # the 5-minute cache has expired
    run(bot.invite_status(private_command(env, run, 132, "/start", username="ana_99"), env.ctx))
    assert env.bot.posting_unlocked(132)
    assert "acum poți posta" in env.bot.send_message.call_args.kwargs["text"]


def test_member_without_username_or_handle_on_a_profile_still_needs_invites(env, run):
    env.site_handles.add("altcineva")
    join(env, run, 133)
    join(env, run, 134, username="ana_99")
    assert not env.bot.posting_unlocked(133) and not env.bot.posting_unlocked(134)


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


def start(env, run, uid, payload):
    upd = private_command(env, run, uid, f"/start {payload}")
    run(bot.cmd_start(upd, SimpleNamespace(bot=env.bot, args=[payload])))


def test_account_help_link_explains_and_alerts_support(env, run):
    start(env, run, 42, "cont")
    texts = [(c.kwargs.get("chat_id") or c.args[0], c.kwargs.get("text") or c.args[1])
             for c in env.bot.send_message.call_args_list]
    assert texts[0][0] == 42 and "numărul de telefon" in texts[0][1]
    assert texts[1][0] == S and "nu își poate face cont" in texts[1][1]
    # An admin replying to that alert reaches the person.
    assert run(env.store.support_user_for(500)) == 42


def test_plain_help_link_does_not_alert_support(env, run):
    start(env, run, 42, "ajutor")
    assert env.bot.send_message.await_count == 1


def test_unknown_start_payload_shows_invite_status(env, run):
    start(env, run, 42, "invite")
    assert "Linkul tău" in env.bot.send_message.call_args.kwargs["text"]


# ---- scheduled posts ----------------------------------------------------
from datetime import datetime, timedelta, timezone

import posts


class FakeHttp:
    """Stands in for httpx.AsyncClient when the bot downloads a profile photo."""
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url):
        return SimpleNamespace(content=b"webp-bytes", raise_for_status=lambda: None)


@pytest.fixture
def daytime(monkeypatch):
    """Freeze 'now' at 14:00 in Bucharest and serve one recommended profile."""
    now = datetime(2026, 10, 5, 14, 0, tzinfo=posts.LOCAL_TZ).astimezone(timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(bot, "datetime", Clock)
    monkeypatch.setattr(bot.httpx, "AsyncClient", FakeHttp)
    rec = posts.Profile("/escorte/ana", "Ana", "Cluj", "https://x/ana.webp")

    async def fake_site(_now):
        return [], None, [rec]
    monkeypatch.setattr(posts, "fetch_site_profiles", fake_site)
    return now


def test_tick_plans_first_then_posts_profile_then_question(env, run, daytime):
    run(bot.run_tick(env.bot))  # first tick only plans
    env.bot.send_photo.assert_not_awaited()
    run(env.store.set_time("next_post_at", daytime - timedelta(minutes=1)))
    run(env.store.set_time("last_human_at", daytime - timedelta(minutes=5)))

    run(bot.run_tick(env.bot))
    photo = env.bot.send_photo.call_args
    assert photo.kwargs["photo"] == b"webp-bytes" and "/escorte/ana" in photo.kwargs["caption"]
    nxt = run(env.store.get_time("next_post_at"))
    assert timedelta(hours=3) <= nxt - daytime <= timedelta(hours=6)

    # Nobody has written since: the next due moment passes in silence.
    run(env.store.set_time("next_post_at", daytime - timedelta(minutes=1)))
    run(bot.run_tick(env.bot))
    assert env.bot.send_photo.await_count == 1 and env.bot.send_message.await_count == 0
    env.bot.send_poll.assert_not_awaited()

    # Someone writes: the next post is a question, not another profile.
    run(env.store.set_time("last_human_at", daytime + timedelta(seconds=1)))
    run(bot.run_tick(env.bot))
    assert env.bot.send_poll.await_count + env.bot.send_message.await_count == 1
    kinds = run(env.store._all("SELECT kind FROM bot_posts ORDER BY id"))
    assert [k["kind"] for k in kinds] == ["recommended", "question"]


def test_profile_posted_recently_is_not_repeated(env, run, daytime):
    # Ana was posted 2h ago and the last post was a question, so a profile is next.
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    run(env.store.record_post(G, "recommended", "/escorte/ana", two_hours_ago))
    run(env.store.record_post(G, "question", "0", two_hours_ago))
    run(env.store.set_time("next_post_at", daytime - timedelta(minutes=1)))
    run(env.store.set_time("last_human_at", daytime))

    run(bot.run_tick(env.bot))
    env.bot.send_photo.assert_not_awaited()  # ana is the only profile and was just posted
    assert env.bot.send_poll.await_count + env.bot.send_message.await_count == 1


def test_group_message_marks_people_as_active(env, run):
    message(env, run, ADMIN_ID, "salut")
    assert run(env.store.get_time("last_human_at")) is not None


def test_tick_endpoint_refuses_without_the_secret(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(bot, "TICK_SECRET", "s" * 20)
    client = TestClient(bot.app)
    assert client.get("/tick").status_code == 403
    assert client.get("/tick", headers={"X-Tick-Secret": "wrong"}).status_code == 403


# ---- whitelist ------------------------------------------------------------
def group_command(env, run, uid, text, reply_to_uid=None, handler=None):
    msg = {"message_id": 30, "date": 0, "from": user(uid), "text": text,
           "chat": {"id": G, "type": "supergroup"},
           "entities": [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]}
    if reply_to_uid:
        msg["reply_to_message"] = {"message_id": 29, "date": 0, "from": user(reply_to_uid),
                                   "chat": {"id": G, "type": "supergroup"}, "text": "hei"}
    upd = Update.de_json({"update_id": 6, "message": msg}, None)
    run((handler or bot.cmd_whitelist)(upd, SimpleNamespace(bot=env.bot, args=text.split()[1:])))


def test_whitelisted_member_skips_every_rule(env, run):
    group_command(env, run, ADMIN_ID, "/whitelist", reply_to_uid=90)
    assert env.bot.posting_unlocked(90)  # a lock or mute she had is lifted
    for mid in range(1, 5):  # locked pre-bot member, repeated ad, phone number
        message(env, run, 90, "Același anunț, sună 0722123456", mid=mid)
    env.bot.delete_message.assert_not_awaited()
    assert events(env, run, 90) == ["whitelist"]


def test_unwhitelist_brings_the_rules_back(env, run):
    group_command(env, run, ADMIN_ID, "/whitelist 90")
    group_command(env, run, ADMIN_ID, "/unwhitelist", reply_to_uid=90)
    message(env, run, 90, "salut")  # never earned her invites: deleted again
    env.bot.delete_message.assert_awaited_with(G, 10)
    assert events(env, run, 90)[:2] == ["whitelist", "unwhitelist"]


def test_only_admins_can_whitelist(env, run):
    group_command(env, run, 91, "/whitelist", reply_to_uid=91)
    assert not run(env.store.is_whitelisted(G, 91))


def test_whitelist_without_a_target_explains_how(env, run):
    group_command(env, run, ADMIN_ID, "/whitelist")
    assert "Dă reply" in env.bot.send_message.call_args.args[1]


def test_whitelisted_person_joining_can_post_straight_away(env, run):
    group_command(env, run, ADMIN_ID, "/whitelist 95")
    env.bot.restrict_chat_member.reset_mock()
    join(env, run, 95)
    assert env.bot.posting_unlocked(95)
    assert not any(c.args[2] is bot.READ_ONLY for c in env.bot.restrict_chat_member.call_args_list)


def test_whitelisted_private_status(env, run):
    group_command(env, run, ADMIN_ID, "/whitelist 96")
    run(bot.invite_status(private_command(env, run, 96, "/status"), env.ctx))
    assert "lista albă" in env.bot.send_message.call_args.kwargs["text"]


# ---- /start menu --------------------------------------------------------
def tap(env, run, uid, key):
    upd = Update.de_json({"update_id": 7, "callback_query": {
        "id": "1", "from": user(uid), "chat_instance": "c", "data": f"menu:{key}",
        "message": {"message_id": 3, "date": 0, "from": BOT_USER, "text": "Salut!",
                    "chat": {"id": uid, "type": "private"}}}}, env.bot)
    run(bot.on_menu(upd, env.ctx))


def last_text(env):
    call = env.bot.send_message.call_args
    return call.kwargs.get("text") or call.args[1]


def test_plain_start_shows_the_help_menu(env, run):
    run(bot.cmd_start(private_command(env, run, 42, "/start"), env.ctx))
    markup = env.bot.send_message.call_args.kwargs["reply_markup"]
    assert [row[0].callback_data for row in markup.inline_keyboard] == [f"menu:{k}" for k, _ in bot.MENU]


def test_menu_answers_sms_on_the_spot_without_alerting_support(env, run):
    tap(env, run, 42, "sms")
    env.bot.answer_callback_query.assert_awaited()
    assert "Continuă cu Google" in last_text(env)
    assert env.bot.send_message.await_count == 1


def test_menu_claim_asks_for_details_and_alerts_support(env, run):
    tap(env, run, 42, "revendicare")
    chats = [c.kwargs.get("chat_id") or c.args[0] for c in env.bot.send_message.call_args_list]
    assert chats == [42, S]


def test_menu_group_shows_her_invite_status(env, run):
    tap(env, run, 42, "grup")
    assert "Linkul tău" in last_text(env)


def test_menu_back_shows_the_menu_again(env, run):
    tap(env, run, 42, "meniu")
    assert last_text(env) == "Salut! Cu ce te putem ajuta?"


# ---- /info and /unlock ----------------------------------------------------
def test_info_explains_why_she_cannot_post(env, run):
    message(env, run, 90, "salut")  # pre-bot member, locked on her first post
    group_command(env, run, ADMIN_ID, "/info", reply_to_uid=90, handler=bot.cmd_info)
    text = last_text(env)
    for part in ("Nu poate posta încă", "înainte de bot", f"Invitații: 0/{bot.INVITES_REQUIRED}",
                 "approape.ro: nu", "delete — fără drept de postare"):
        assert part in text, part


def test_info_and_unlock_are_admin_only(env, run):
    group_command(env, run, 77, "/info 90", handler=bot.cmd_info)
    group_command(env, run, 77, "/unlock 90", handler=bot.cmd_unlock)
    env.bot.send_message.assert_not_awaited()
    env.bot.restrict_chat_member.assert_not_awaited()


def test_unlock_lets_her_post_but_keeps_the_other_rules(env, run):
    group_command(env, run, ADMIN_ID, "/unlock 91", handler=bot.cmd_unlock)
    assert env.bot.posting_unlocked(91)
    assert run(env.store.get_member(G, 91))["unlocked"]
    message(env, run, 91, "Anunț: sună la 0722123456", mid=1)
    env.bot.delete_message.assert_not_awaited()
    message(env, run, 91, "Anunț: sună la 0722123456", mid=2)  # same ad again
    env.bot.delete_message.assert_awaited_with(G, 2)
    assert events(env, run, 91) == ["unlock", "delete"]


def test_health_reports_the_live_commit(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("RENDER_GIT_COMMIT", "ab27b5d0123456789")
    assert TestClient(bot.app).get("/").json()["commit"] == "ab27b5d"

