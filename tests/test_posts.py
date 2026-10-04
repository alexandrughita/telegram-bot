import random
from datetime import datetime, timedelta, timezone

import posts
from posts import Profile

UTC = timezone.utc


def local(hour, minute=0, day=5):
    """A moment at Bucharest local time on 2026-10-<day> (UTC+3)."""
    return datetime(2026, 10, day, hour, minute, tzinfo=posts.LOCAL_TZ).astimezone(UTC)


class FixedRandom:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value

    def choice(self, items):
        return items[0]


def test_next_post_is_three_to_six_hours_later_in_daytime():
    assert posts.next_post_time(local(10), FixedRandom(0)) == local(13)
    assert posts.next_post_time(local(10), FixedRandom(0.999999)).astimezone(posts.LOCAL_TZ).hour == 15


def test_posts_may_land_until_one_in_the_morning():
    nxt = posts.next_post_time(local(21), FixedRandom(0)).astimezone(posts.LOCAL_TZ)  # 00:00 stays
    assert (nxt.day, nxt.hour) == (6, 0)


def test_next_post_that_would_land_in_quiet_hours_moves_to_ten():
    nxt = posts.next_post_time(local(23), FixedRandom(0)).astimezone(posts.LOCAL_TZ)  # 02:00 -> 10:00
    assert (nxt.day, nxt.hour) == (6, 10)
    nxt = posts.next_post_time(local(1), FixedRandom(0.5)).astimezone(posts.LOCAL_TZ)  # 05:30 -> same day
    assert (nxt.day, nxt.hour) == (5, 11)


def test_posts_land_in_daytime_whatever_the_dice_say():
    rng = random.Random(1)
    now = local(9)
    for _ in range(200):
        now = posts.next_post_time(now, rng)
        hour = now.astimezone(posts.LOCAL_TZ).hour
        assert hour >= 10 or hour < 1


def test_due_needs_the_time_daylight_and_someone_who_wrote_since():
    now = local(14)
    earlier = now - timedelta(hours=1)
    assert posts.due(now, earlier, last_post_at=None, last_human_at=earlier)
    assert not posts.due(now, now + timedelta(minutes=1), None, earlier)       # not yet
    assert not posts.due(local(2), local(1), None, local(1))                    # quiet hours
    assert not posts.due(local(9, 59), local(1), None, local(1))
    assert posts.due(local(0, 30), local(0), None, local(0, 10))                # still allowed
    assert not posts.due(now, earlier, last_post_at=earlier, last_human_at=earlier - timedelta(minutes=5))
    assert not posts.due(now, earlier, None, last_human_at=None)                # nobody ever wrote


def profile(path, created=None, photo="https://x/p.webp"):
    return Profile(path=path, name="Ana", city="Cluj", photo=photo, created_at=created)


def test_profile_order_new_then_top_then_recommended():
    now = local(12)
    new = [profile("/escorte/nou", created=now - timedelta(days=3))]
    top = profile("/escorte/top")
    rec = [profile("/escorte/rec")]
    assert posts.pick_profile(new, top, rec, set(), now, FixedRandom(0)) == ("new", new[0])
    assert posts.pick_profile(new, top, rec, {"/escorte/nou"}, now, FixedRandom(0)) == ("top", top)
    assert posts.pick_profile(new, top, rec, {"/escorte/nou", "/escorte/top"}, now, FixedRandom(0)) == ("recommended", rec[0])
    assert posts.pick_profile(new, top, rec, {"/escorte/nou", "/escorte/top", "/escorte/rec"}, now) == (None, None)


def test_old_or_photoless_profiles_are_not_new():
    now = local(12)
    old = profile("/escorte/vechi", created=now - timedelta(days=40))
    no_photo = profile("/escorte/fara-poza", created=now, photo="")
    assert posts.pick_profile([old, no_photo], None, [], set(), now) == (None, None)


PRIORITY = {str(i) for i, q in enumerate(posts.QUESTIONS) if q.get("priority")}
REGULAR = [i for i, q in enumerate(posts.QUESTIONS) if not q.get("priority")]


def test_question_is_not_repeated_until_all_were_used():
    used = {str(i) for i in REGULAR[:-1]}
    assert posts.pick_question(used, PRIORITY) == REGULAR[-1]
    assert posts.pick_question({str(i) for i in REGULAR}, PRIORITY) in REGULAR


def test_priority_questions_come_first_when_due():
    assert len(PRIORITY) == 2
    first = posts.pick_question(set(), set())
    assert str(first) in PRIORITY
    second = posts.pick_question({str(first)}, {str(first)})
    assert str(second) in PRIORITY and second != first
    # Both posted within PRIORITY_REPEAT_DAYS: back to the regular rotation.
    assert posts.pick_question(PRIORITY, PRIORITY) in REGULAR


def test_polls_fit_telegram_limits():
    for q in posts.QUESTIONS:
        if "poll" in q:
            assert len(q["poll"]) <= 300 and 2 <= len(q["options"]) <= 10
            assert all(len(o) <= 100 for o in q["options"])


def test_caption_escapes_and_tracks_the_link():
    caption = posts.profile_caption("new", Profile("/creatoare/ana", "Ana <3", "Iași", "https://x"))
    assert "Ana &lt;3" in caption and "Iași" in caption
    assert 'href="https://www.approape.ro/creatoare/ana?utm_source=telegram' in caption


def test_firestore_fields_become_a_profile():
    fields = {"slug": "ana", "role": "creatoare", "displayName": " Ana ", "currentCity": "Iași",
              "photos": [{"url": "https://x/1.webp"}, "https://x/2.webp"]}
    p = posts.profile_from_fields(fields)
    assert (p.path, p.name, p.city, p.photo) == ("/creatoare/ana", "Ana", "Iași", "https://x/1.webp")


def test_away_hidden_or_inactive_profiles_are_skipped():
    now = local(12)
    assert posts.is_listed_now({}, now)
    assert not posts.is_listed_now({"isHidden": True}, now)
    assert not posts.is_listed_now({"isActive": False}, now)
    assert not posts.is_listed_now({"awayMode": True}, now)
    assert not posts.is_listed_now({"awayMode": True, "awayUntil": now + timedelta(days=1)}, now)
    assert posts.is_listed_now({"awayMode": True, "awayUntil": now - timedelta(days=1)}, now)


def test_sitemap_paths():
    xml = "<url><loc>https://www.approape.ro/escorte/ana</loc></url><url><loc>https://www.approape.ro/creatoare/bia</loc></url>"
    assert posts.sitemap_paths(xml) == {"/escorte/ana", "/creatoare/bia"}


def test_telegram_handle_reads_every_way_a_profile_writes_it():
    for raw in ("@Ana_99", "Ana_99", "https://t.me/ana_99", "t.me/ana_99/", "telegram.me/Ana_99?x=1",
                " https://www.t.me/ana_99 "):
        assert posts.telegram_handle(raw) == "ana_99", raw
    for raw in ("t.me/+abcDEF123", "+40712345678", "https://t.me/joinchat/xyz", "ana", "", None):
        assert posts.telegram_handle(raw) is None, raw

