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
    bot._support_ack_at.clear()
    bot._moderation_lock = asyncio.Lock()  # each test runs on its own event loop
    bot._tick_lock = asyncio.Lock()
    fake = FakeBot()
    yield SimpleNamespace(bot=fake, store=store, ctx=SimpleNamespace(bot=fake, args=[]))
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
    for invited in (120, 121, 122):
        join(env, run, invited, by=110)
    assert run(env.store.invite_count(G, 110)) == 3
    env.bot.restrict_chat_member.assert_not_awaited()


def test_someone_added_by_hand_counts_once_for_whoever_brought_her_first(env, run):
    join(env, run, 120, by=110)
    join(env, run, 120, old="left", by=111)  # leaves, another member adds her again
    assert run(env.store.invite_count(G, 110)) == 1
    assert run(env.store.invite_count(G, 111)) == 0










def test_new_member_can_post_without_invites(env, run):
    join(env, run, 80)
    env.bot.restrict_chat_member.assert_not_awaited()
    env.bot.send_message.assert_not_awaited()  # no welcome asking for invites
    message(env, run, 80, "salut")
    env.bot.delete_message.assert_not_awaited()


def test_joining_through_her_link_still_credits_her(env, run):
    run(bot.invite_status(private_command(env, run, 50, "/invite"), env.ctx))
    link = run(env.store.get_invite_link(G, 50))
    assert link in env.bot.send_message.call_args.kwargs["text"]
    join(env, run, 61, link=link)
    assert run(env.store.invite_count(G, 50)) == 1
    assert "Ai adus: 0" in env.bot.send_message.call_args.kwargs["text"]


def test_member_from_before_the_bot_can_post(env, run):
    message(env, run, 90, "Bună tuturor, sunt aici de mult timp")
    env.bot.delete_message.assert_not_awaited()
    env.bot.restrict_chat_member.assert_not_awaited()


def test_members_the_old_invite_rule_locked_are_lifted_once(env, run):
    run(env.store.add_member(G, SimpleNamespace(id=90, username=None, first_name="U90"), legacy=True))
    posting_member(env, run, 91)
    run(bot.lift_invite_locks(env.bot))
    assert env.bot.posting_unlocked(90) and not env.bot.posting_unlocked(91)
    assert run(env.store.get_member(G, 90))["unlocked"]
    assert events(env, run, 90) == ["unlock"]
    run(bot.lift_invite_locks(env.bot))  # every restart: nothing left to lift
    assert env.bot.restrict_chat_member.await_count == 1






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


def test_gifs_are_not_limited(env, run):
    posting_member(env, run, 90)
    for mid in (1, 2, 3):
        gif = {"file_id": "a", "file_unique_id": f"gif{mid}", "width": 1, "height": 1, "duration": 1}
        message(env, run, 90, mid=mid, animation=gif, document={"file_id": "a", "file_unique_id": f"gif{mid}"})
    env.bot.delete_message.assert_not_awaited()


AD = "Acesta este un anunt suficient de lung"


def test_the_same_text_is_allowed_twice_a_day_counting_the_first(env, run):
    posting_member(env, run, 90)
    message(env, run, 90, AD, mid=1)
    message(env, run, 90, AD, mid=2)
    assert run(env.store.recent_ad_count(G, 90, 24)) == 2
    message(env, run, 90, AD, mid=3)
    env.bot.delete_message.assert_awaited_once_with(G, 3)
    assert "limita de 2 reclame" in last_text(env)
    assert "Mesajele adevărate" in last_text(env)
    assert run(env.store._one("SELECT COUNT(*) AS n FROM violations"))["n"] == 0


def test_the_same_text_hours_apart_still_counts(env, run):
    # The duplicate check only looks back 6h; the ad limit must see the whole day.
    posting_member(env, run, 90)
    message(env, run, 90, AD, mid=1)
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '7 hours'"))
    message(env, run, 90, AD, mid=2)
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '7 hours' WHERE message_id = 2"))
    message(env, run, 90, AD, mid=3)
    env.bot.delete_message.assert_awaited_once_with(G, 3)


def test_the_same_text_is_allowed_again_after_a_day(env, run):
    posting_member(env, run, 90)
    message(env, run, 90, AD, mid=1)
    message(env, run, 90, AD, mid=2)
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '25 hours'"))
    message(env, run, 90, AD, mid=3)
    env.bot.delete_message.assert_not_awaited()


def test_reworded_copies_are_the_same_ad(env, run):
    # Real variants one member posted on 2026-10-05; only exact copies counted then.
    posting_member(env, run, 90)
    message(env, run, 90, "Doamne/domnișoare nesatisfăcute, aștept mesaj în privat", mid=1)
    message(env, run, 90, "Doamne/domnișoare nesatisfăcute din Brașov, pm me", mid=2)
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '10 hours'"))
    message(env, run, 90, "Doamne/domnișoare nesatisfăcute pm me", mid=3)
    env.bot.delete_message.assert_awaited_once_with(G, 3)
    assert "limita de 2 reclame" in last_text(env)
    assert run(env.store._one("SELECT COUNT(*) AS n FROM violations"))["n"] == 0


def test_a_font_swap_is_the_same_text(env, run):
    posting_member(env, run, 90)
    message(env, run, 90, "𝐃𝐢𝐬𝐩𝐨𝐧𝐢𝐛𝐢𝐥𝐚 𝐩𝐞𝐧𝐭𝐫𝐮 videocall și sexting", mid=1)
    message(env, run, 90, "Disponibila pentru videocall și sexting", mid=2)
    assert run(env.store.recent_ad_count(G, 90, 24)) == 2


def test_different_messages_from_one_member_are_not_ads(env, run):
    posting_member(env, run, 90)
    message(env, run, 90, "Bună dimineața tuturor, ce mai faceți azi?", mid=1)
    message(env, run, 90, "Știe cineva un restaurant bun în Cluj pentru diseară?", mid=2)
    assert run(env.store.recent_ad_count(G, 90, 24)) == 0


def test_short_or_emoji_repeats_are_not_ads(env, run):
    posting_member(env, run, 90)
    for mid in (1, 2, 3):
        message(env, run, 90, "🔥🔥🔥❤️❤️", mid=mid)
        message(env, run, 90, "mersi", mid=mid + 10)
    env.bot.delete_message.assert_not_awaited()


def test_two_animated_sticker_messages_a_day(env, run):
    posting_member(env, run, 90)
    sticker = {"file_id": "s", "file_unique_id": "st1", "width": 512, "height": 512,
               "type": "regular", "is_animated": True, "is_video": False}
    message(env, run, 90, mid=1, sticker=sticker)
    message(env, run, 90, mid=2, sticker={**sticker, "file_unique_id": "st2"})
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '23 hours'"))
    message(env, run, 90, mid=3, sticker={**sticker, "file_unique_id": "st3", "is_animated": False, "is_video": True})
    env.bot.delete_message.assert_awaited_once_with(G, 3)
    notice = env.bot.send_message.call_args.args[1]
    assert "2 reclame pe zi" in notice and "scrie-ne" in notice
    assert events(env, run, 90) == ["delete"]  # not a violation: no warning, no mute
    assert run(env.store._one("SELECT COUNT(*) AS n FROM violations"))["n"] == 0


def test_animated_sticker_allowed_again_after_a_day(env, run):
    posting_member(env, run, 90)
    sticker = {"file_id": "s", "file_unique_id": "st1", "width": 512, "height": 512,
               "type": "regular", "is_animated": True, "is_video": False}
    message(env, run, 90, mid=1, sticker=sticker)
    message(env, run, 90, mid=2, sticker={**sticker, "file_unique_id": "st2"})
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '25 hours'"))
    message(env, run, 90, mid=3, sticker={**sticker, "file_unique_id": "st3"})
    env.bot.delete_message.assert_not_awaited()


def test_two_static_stickers_a_day(env, run):
    posting_member(env, run, 90)
    sticker = {"file_id": "s", "file_unique_id": "st1", "width": 512, "height": 512,
               "type": "regular", "is_animated": False, "is_video": False}
    message(env, run, 90, mid=1, sticker=sticker)
    message(env, run, 90, mid=2, sticker={**sticker, "file_unique_id": "st2"})
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '23 hours'"))
    message(env, run, 90, mid=3, sticker={**sticker, "file_unique_id": "st3"})
    env.bot.delete_message.assert_awaited_once_with(G, 3)
    notice = env.bot.send_message.call_args.args[1]
    assert "2 reclame pe zi" in notice and "scrie-ne" in notice
    assert events(env, run, 90) == ["delete"]  # not a violation: no warning, no mute
    assert run(env.store._one("SELECT COUNT(*) AS n FROM violations"))["n"] == 0


def test_static_and_animated_sticker_limits_are_separate(env, run):
    posting_member(env, run, 90)
    sticker = {"file_id": "s", "file_unique_id": "st1", "width": 512, "height": 512,
               "type": "regular", "is_animated": True, "is_video": False}
    message(env, run, 90, mid=1, sticker=sticker)
    message(env, run, 90, mid=2, sticker={**sticker, "file_unique_id": "st2"})
    message(env, run, 90, mid=3, sticker={**sticker, "file_unique_id": "st3", "is_animated": False})
    message(env, run, 90, mid=4, sticker={**sticker, "file_unique_id": "st4", "is_animated": False})
    env.bot.delete_message.assert_not_awaited()


def test_static_sticker_allowed_again_after_a_day(env, run):
    posting_member(env, run, 90)
    sticker = {"file_id": "s", "file_unique_id": "st1", "width": 512, "height": 512,
               "type": "regular", "is_animated": False, "is_video": False}
    message(env, run, 90, mid=1, sticker=sticker)
    message(env, run, 90, mid=2, sticker={**sticker, "file_unique_id": "st2"})
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '25 hours'"))
    message(env, run, 90, mid=3, sticker={**sticker, "file_unique_id": "st3"})
    env.bot.delete_message.assert_not_awaited()


def test_a_message_with_an_approape_link_is_exempt_from_every_rule(env, run):
    posting_member(env, run, 90)
    for mid in range(1, 4):
        message(env, run, 90, "Profilul meu: https://www.approape.ro/escorte/ana sună 0722123456", mid=mid)
    message(env, run, 90, "detalii", mid=4,
            entities=[{"type": "text_link", "offset": 0, "length": 7, "url": "https://approape.ro/escorte/ana"}])
    # Telegram marks a bare "approape.ro/..." as a url entity itself.
    message(env, run, 91, "Nouă aici, vezi approape.ro/creatoare/ana", mid=5,
            entities=[{"type": "url", "offset": 16, "length": 25}])
    env.bot.delete_message.assert_not_awaited()
    assert run(env.store.recent_ad_count(G, 90, 24)) == 0


def test_a_lookalike_domain_is_not_approape(env, run):
    posting_member(env, run, 90)
    for mid in (1, 2):
        message(env, run, 90, "Vezi https://approape.ro.example.com/x", mid=mid)
    env.bot.delete_message.assert_awaited_once_with(G, 2)


def test_stickers_arriving_together_are_still_limited(env, run):
    # Telegram delivers a backlog (e.g. when Render wakes up) over parallel
    # webhook requests, so the handlers run concurrently.
    posting_member(env, run, 90)
    updates = [Update.de_json({"update_id": mid, "message": {
        "message_id": mid, "date": 0, "from": user(90), "chat": {"id": G, "type": "supergroup"},
        "sticker": {"file_id": "s", "file_unique_id": f"st{mid}", "width": 512, "height": 512,
                    "type": "regular", "is_animated": False, "is_video": False}}}, None) for mid in range(1, 5)]

    async def burst():
        await asyncio.gather(*(bot.on_group_message(u, env.ctx) for u in updates))
    run(burst())
    assert env.bot.delete_message.await_count == 2


def forwarded(uid, mid, text=None, **extra):
    """A message she forwarded from someone else (here: a channel)."""
    msg = {"message_id": mid, "date": 0, "from": user(uid), "chat": {"id": G, "type": "supergroup"},
           "forward_origin": {"type": "channel", "date": 0, "message_id": 7,
                              "chat": {"id": -1009, "type": "channel", "title": "c"}}, **extra}
    if text is not None:
        msg["text"] = text
    return Update.de_json({"update_id": mid, "message": msg}, None)


def test_forwarding_text_someone_else_posted_is_an_ad(env, run):
    posting_member(env, run, 90)
    posting_member(env, run, 91)
    message(env, run, 90, AD, mid=1)
    run(bot.on_group_message(forwarded(91, 2, AD), env.ctx))
    assert run(env.store.recent_ad_count(G, 91, 24)) == 1
    assert run(env.store.recent_ad_count(G, 90, 24)) == 0  # the original is not her ad
    env.bot.delete_message.assert_not_awaited()


def test_forwarded_repeats_hit_the_ad_limit(env, run):
    posting_member(env, run, 90)
    posting_member(env, run, 91)
    message(env, run, 90, AD, mid=1)
    message(env, run, 90, "Alt anunt la fel de lung ca primul", mid=2)
    message(env, run, 90, "Al treilea anunt, tot destul de lung", mid=3)
    run(bot.on_group_message(forwarded(91, 4, AD), env.ctx))
    run(bot.on_group_message(forwarded(91, 5, "Alt anunt la fel de lung ca primul"), env.ctx))
    run(bot.on_group_message(forwarded(91, 6, "Al treilea anunt, tot destul de lung"), env.ctx))
    env.bot.delete_message.assert_awaited_once_with(G, 6)
    assert "limita de 2 reclame" in last_text(env)


def test_forwarding_text_seen_more_than_a_day_ago_is_not_an_ad(env, run):
    posting_member(env, run, 90)
    posting_member(env, run, 91)
    message(env, run, 90, AD, mid=1)
    run(env.store._execute("UPDATE messages SET created_at = now() - interval '25 hours'"))
    run(bot.on_group_message(forwarded(91, 2, AD), env.ctx))
    assert run(env.store.recent_ad_count(G, 91, 24)) == 0


def test_short_forwards_and_new_forwards_are_not_ads(env, run):
    posting_member(env, run, 90)
    posting_member(env, run, 91)
    message(env, run, 90, "mersi mult", mid=1)
    run(bot.on_group_message(forwarded(91, 2, "mersi mult"), env.ctx))
    run(bot.on_group_message(forwarded(91, 3, "Ceva nou, nemaivazut in grup pana acum"), env.ctx))
    assert run(env.store.recent_ad_count(G, 91, 24)) == 0


def test_text_someone_else_posted_is_not_an_ad_unless_forwarded(env, run):
    # Typing the same answer as someone else ("multumesc pentru recomandare") is chat, not an ad.
    posting_member(env, run, 90)
    posting_member(env, run, 91)
    message(env, run, 90, AD, mid=1)
    message(env, run, 91, AD, mid=2)
    assert run(env.store.recent_ad_count(G, 91, 24)) == 0


def test_forwarded_stickers_follow_the_sticker_limit(env, run):
    posting_member(env, run, 90)
    for mid in (1, 2, 3):
        sticker = {"file_id": "s", "file_unique_id": f"st{mid}", "width": 512, "height": 512,
                   "type": "regular", "is_animated": False, "is_video": False}
        run(bot.on_group_message(forwarded(90, mid, sticker=sticker), env.ctx))
    env.bot.delete_message.assert_awaited_once_with(G, 3)


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
    for mid in range(1, 5):  # repeated ad, phone number
        message(env, run, 90, "Același anunț, sună 0722123456", mid=mid)
    env.bot.delete_message.assert_not_awaited()
    assert events(env, run, 90) == ["whitelist"]


def test_unwhitelist_brings_the_rules_back(env, run):
    group_command(env, run, ADMIN_ID, "/whitelist 90")
    group_command(env, run, ADMIN_ID, "/unwhitelist", reply_to_uid=90)
    for mid in (1, 2, 3):
        message(env, run, 90, AD, mid=mid)
    env.bot.delete_message.assert_awaited_once_with(G, 3)
    assert events(env, run, 90)[:2] == ["whitelist", "unwhitelist"]


def test_only_admins_can_whitelist(env, run):
    group_command(env, run, 91, "/whitelist", reply_to_uid=91)
    assert not run(env.store.is_whitelisted(G, 91))


def test_whitelist_without_a_target_explains_how(env, run):
    group_command(env, run, ADMIN_ID, "/whitelist")
    assert "Dă reply" in env.bot.send_message.call_args.args[1]




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
def test_info_shows_her_ads_and_invites(env, run):
    posting_member(env, run, 90)
    message(env, run, 90, AD, mid=1)
    message(env, run, 90, AD, mid=2)
    group_command(env, run, ADMIN_ID, "/info", reply_to_uid=90, handler=bot.cmd_info)
    text = last_text(env)
    for part in ("A adus: 0", "Reclame (24h): 2/2", "Stickere (24h): statice 0/2 · animate 0/2",
                 "Abateri (48h): 0 → următoarea: ștergere"):
        assert part in text, part


def test_info_says_she_can_post(env, run):
    posting_member(env, run, 91)
    group_command(env, run, ADMIN_ID, "/info", reply_to_uid=91, handler=bot.cmd_info)
    assert "✅ Poate posta acum." in last_text(env)


def test_info_says_when_ads_are_allowed_again_and_lists_the_block(env, run):
    posting_member(env, run, 92)
    for mid in (1, 2, 3):
        message(env, run, 92, AD, mid=mid)
    group_command(env, run, ADMIN_ID, "/info", reply_to_uid=92, handler=bot.cmd_info)
    text = last_text(env)
    assert "⛔ Limita de reclame atinsă: mesaje normale da, reclame din nou" in text
    assert "Ultimele acțiuni:" in text and "șters: reclame: limita 2/24h" in text


def test_info_shows_a_mute_with_its_end(env, run):
    posting_member(env, run, 93)
    until = datetime.now(timezone.utc) + timedelta(minutes=30)
    env.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(
        status="restricted", can_send_messages=False, until_date=until,
        user=SimpleNamespace(id=93, username=None, first_name="U93", is_bot=False)))
    group_command(env, run, ADMIN_ID, "/info", reply_to_uid=93, handler=bot.cmd_info)
    assert f"🔇 Mute până {bot.local_when(until, datetime.now(timezone.utc))}." in last_text(env)


def test_info_and_unlock_are_admin_only(env, run):
    group_command(env, run, 77, "/info 90", handler=bot.cmd_info)
    group_command(env, run, 77, "/unlock 90", handler=bot.cmd_unlock)
    env.bot.send_message.assert_not_awaited()
    env.bot.restrict_chat_member.assert_not_awaited()


def test_unlock_lifts_a_mute_but_keeps_the_other_rules(env, run):
    group_command(env, run, ADMIN_ID, "/unlock 91", handler=bot.cmd_unlock)
    assert env.bot.posting_unlocked(91)
    for mid in (1, 2, 3):
        message(env, run, 91, AD, mid=mid)
    env.bot.delete_message.assert_awaited_once_with(G, 3)
    assert events(env, run, 91) == ["unlock", "delete"]


def test_health_reports_the_live_commit(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("RENDER_GIT_COMMIT", "ab27b5d0123456789")
    assert TestClient(bot.app).get("/").json()["commit"] == "ab27b5d"




def hit_ad_limit(env, run, uid, hours_ago, ads_still_in_window):
    """She was blocked hours_ago; ads_still_in_window of her ads are under 24h old."""
    now = datetime.now(timezone.utc)
    run(env.store.log_event(G, uid, "delete", "reclame: limita 2/24h"))
    run(env.store.set_time(f"ad_limit:{G}:{uid}", now - timedelta(hours=hours_ago)))
    run(env.store._execute("DELETE FROM messages WHERE user_id=%s", (uid,)))
    for n in range(ads_still_in_window):
        run(env.store._execute(
            "INSERT INTO messages(chat_id,user_id,message_id,text,urls,phones,media,is_ad) "
            "VALUES(%s,%s,%s,'ad','{}','{}','{}',true)", (G, uid, 900 + n)))


def reminders_sent(env, uid):
    return sum(1 for c in env.bot.send_message.call_args_list if c.args[0] == uid)


def test_ad_reminder_waits_until_the_limit_lifts(env, run):
    hit_ad_limit(env, run, 50, hours_ago=1, ads_still_in_window=2)
    run(bot.run_ad_reminders(env.bot))
    assert reminders_sent(env, 50) == 0

    hit_ad_limit(env, run, 50, hours_ago=20, ads_still_in_window=1)
    run(bot.run_ad_reminders(env.bot))
    assert reminders_sent(env, 50) == 1


def test_ad_reminder_is_sent_once_not_on_every_tick(env, run):
    hit_ad_limit(env, run, 51, hours_ago=30, ads_still_in_window=0)
    for _ in range(3):
        run(bot.run_ad_reminders(env.bot))
    assert reminders_sent(env, 51) == 1


def test_ad_reminder_at_most_once_a_week(env, run):
    now = datetime.now(timezone.utc)
    # Reminded 3 days ago, blocked again since and the limit has lifted: same week, no reminder.
    run(env.store.set_time(f"ad_reminder:{G}:52", now - timedelta(days=3)))
    hit_ad_limit(env, run, 52, hours_ago=30, ads_still_in_window=0)
    run(bot.run_ad_reminders(env.bot))
    assert reminders_sent(env, 52) == 0

    # A week after the last reminder, a new block earns another one.
    run(env.store.set_time(f"ad_reminder:{G}:52", now - timedelta(days=7.1)))
    run(bot.run_ad_reminders(env.bot))
    assert reminders_sent(env, 52) == 1
