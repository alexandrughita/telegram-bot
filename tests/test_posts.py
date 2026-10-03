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


def test_next_post_that_would_land_at_night_moves_to_the_next_morning():
    nxt = posts.next_post_time(local(21), FixedRandom(0)).astimezone(posts.LOCAL_TZ)  # 00:00 -> morning
    assert (nxt.day, nxt.hour) == (6, 10)
    nxt = posts.next_post_time(local(1), FixedRandom(0.5)).astimezone(posts.LOCAL_TZ)  # 05:30 -> same day
    assert (nxt.day, nxt.hour) == (5, 11)


def test_posts_land_in_daytime_whatever_the_dice_say():
    rng = random.Random(1)
    now = local(9)
    for _ in range(200):
        now = posts.next_post_time(now, rng)
        assert posts.DAY_START_HOUR <= now.astimezone(posts.LOCAL_TZ).hour < posts.DAY_END_HOUR


def test_due_needs_the_time_daylight_and_someone_who_wrote_since():
    now = local(14)
    earlier = now - timedelta(hours=1)
    assert posts.due(now, earlier, last_post_at=None, last_human_at=earlier)
    assert not posts.due(now, now + timedelta(minutes=1), None, earlier)       # not yet
    assert not posts.due(local(2), local(1), None, local(1))                    # night
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


def test_question_is_not_repeated_until_all_were_used():
    used = {str(i) for i in range(len(posts.QUESTIONS) - 1)}
    assert posts.pick_question(used) == len(posts.QUESTIONS) - 1
    assert 0 <= posts.pick_question({str(i) for i in range(len(posts.QUESTIONS))}) < len(posts.QUESTIONS)


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
