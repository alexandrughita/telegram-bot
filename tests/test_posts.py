import random
from datetime import datetime, timedelta, timezone

import posts
from posts import Profile

UTC = timezone.utc


def local(hour, minute=0, day=5):
    return datetime(2026, 10, day, hour, minute, tzinfo=posts.LOCAL_TZ).astimezone(UTC)


class FixedRandom:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value

    def choice(self, items):
        return items[0]


def test_three_daily_slots_are_bucharest_local_time():
    now = local(10)
    assert posts.next_post_time(now, 0, FixedRandom(0)) == local(11)
    assert posts.next_post_time(now, 1, FixedRandom(0)) == local(16)
    assert posts.next_post_time(now, 2, FixedRandom(0)) == local(21)
    assert posts.next_post_time(now, 3, FixedRandom(0)) == local(11, day=6)


def test_late_start_moves_to_next_remaining_slot_without_backfill_burst():
    assert posts.next_post_time(local(17), 0, FixedRandom(0)) == local(21)
    assert posts.next_post_time(local(22), 0, FixedRandom(0)) == local(11, day=6)


def test_due_needs_only_the_scheduled_time_and_daylight_window():
    now = local(14)
    earlier = now - timedelta(hours=1)
    assert posts.due(now, earlier, last_post_at=earlier, last_human_at=None)
    assert not posts.due(now, now + timedelta(minutes=1), None, earlier)
    assert not posts.due(local(2), local(1), None, local(1))
    assert not posts.due(local(9, 59), local(1), None, local(1))
    assert posts.due(local(0, 30), local(0), None, None)


def profile(path, created=None, photo="https://x/p.webp", confirmed=False):
    return Profile(path=path, name="Ana", city="Cluj", photo=photo,
                   created_at=created, whatsapp_confirmed=confirmed)


def test_profile_order_new_then_top_then_recommended():
    now = local(12)
    new = [profile("/escorte/nou", created=now - timedelta(days=3))]
    top = profile("/escorte/top")
    rec = [profile("/escorte/rec")]
    assert posts.pick_profile(new, top, rec, set(), now, FixedRandom(0)) == ("new", new[0])
    assert posts.pick_profile(new, top, rec, {"/escorte/nou"}, now, FixedRandom(0)) == ("top", top)
    assert posts.pick_profile(new, top, rec, {"/escorte/nou", "/escorte/top"}, now, FixedRandom(0)) == ("recommended", rec[0])
    assert posts.pick_profile(new, top, rec, {"/escorte/nou", "/escorte/top", "/escorte/rec"}, now) == (None, None)


def test_contact_confirmation_is_required_when_requested():
    now = local(12)
    unconfirmed = profile("/escorte/fara", created=now - timedelta(days=1))
    confirmed = profile("/escorte/confirmat", created=now - timedelta(days=1), confirmed=True)
    assert posts.pick_profile([unconfirmed, confirmed], None, [], set(), now,
                              FixedRandom(0), require_whatsapp=True) == ("new", confirmed)
    assert posts.pick_profile([unconfirmed], None, [], set(), now,
                              require_whatsapp=True) == (None, None)


def test_old_or_photoless_profiles_are_not_new():
    now = local(12)
    old = profile("/escorte/vechi", created=now - timedelta(days=40))
    no_photo = profile("/escorte/fara-poza", created=now, photo="")
    assert posts.pick_profile([old, no_photo], None, [], set(), now) == (None, None)


def test_caption_marks_confirmed_contact_and_tracks_the_link():
    caption = posts.profile_caption("new", profile("/creatoare/ana", confirmed=True))
    assert "Contact WhatsApp confirmat" in caption
    assert '<a href="https://www.approape.ro/creatoare/ana?utm_source=telegram' in caption
    assert "Ia legătura cu ea</a>" in caption
    assert "❤️ Profil nou" in caption


def test_firestore_fields_become_a_profile():
    fields = {"slug": "ana", "role": "creatoare", "displayName": " Ana ", "currentCity": "Iași",
              "photos": [{"url": "https://x/1.webp"}, "https://x/2.webp"], "whatsappConfirmed": True}
    p = posts.profile_from_fields(fields)
    assert (p.path, p.name, p.city, p.photo, p.whatsapp_confirmed) == (
        "/creatoare/ana", "Ana", "Iași", "https://x/1.webp", True)


def eligible_fields(**overrides):
    fields = {
        "slug": "ana", "role": "escorte", "displayName": "Ana",
        "photoURL": "https://x/p.webp", "bio": "Descriere suficient de lunga " * 3,
        "citySlug": "cluj", "isActive": True,
    }
    fields.update(overrides)
    return fields


def test_sitemap_eligibility_requires_public_quality_profile():
    now = local(12)
    assert posts.is_indexable_profile(eligible_fields())
    assert not posts.is_indexable_profile(eligible_fields(bio="prea scurt"))
    assert not posts.is_indexable_profile(eligible_fields(photoURL=""))
    assert not posts.is_indexable_profile(eligible_fields(citySlug=""))
    assert not posts.is_indexable_profile(eligible_fields(role="client"))
    assert not posts.is_indexable_profile(eligible_fields(slug="adela-alba-iulia"))
    assert not posts.is_indexable_profile(eligible_fields(status="private"))
    assert posts.is_listed_now(eligible_fields(), now)


def test_away_hidden_or_inactive_profiles_are_skipped():
    now = local(12)
    base = eligible_fields()
    assert not posts.is_listed_now({**base, "isHidden": True}, now)
    assert not posts.is_listed_now({**base, "isActive": False}, now)
    assert not posts.is_listed_now({**base, "awayMode": True}, now)
    assert not posts.is_listed_now({**base, "awayMode": True, "awayUntil": now + timedelta(days=1)}, now)
    assert posts.is_listed_now({**base, "awayMode": True, "awayUntil": now - timedelta(days=1)}, now)




def test_firestore_candidate_scan_reads_multiple_pages(monkeypatch):
    calls = []

    class FakeResponse:
        is_error = False
        status_code = 200
        text = ""

        def __init__(self, payload):
            self.payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self.payload

    class DummyClient:
        async def get(self, url, params):
            calls.append(params.copy())
            if len(calls) == 1:
                return FakeResponse({
                    "documents": [
                        {"fields": {"slug": {"stringValue": f"profile-{i}"}}}
                        for i in range(2)
                    ],
                    "nextPageToken": "next-page",
                })
            return FakeResponse({
                "documents": [
                    {"fields": {"slug": {"stringValue": "last-profile"}}}
                ]
            })

    import asyncio
    rows = asyncio.run(posts._query_all_escorts(DummyClient(), page_size=2))
    assert calls == [{"pageSize": 2}, {"pageSize": 2, "pageToken": "next-page"}]
    assert [row["slug"] for row in rows] == ["profile-0", "profile-1", "last-profile"]


def test_firestore_candidate_scan_filters_and_sorts_locally(monkeypatch):
    import asyncio

    class FakeResponse:
        is_error = False
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {
                "documents": [
                    {"fields": {"slug": {"stringValue": "older"},
                                "createdAt": {"timestampValue": "2026-01-01T00:00:00Z"},
                                "whatsappConfirmed": {"booleanValue": True}}},
                    {"fields": {"slug": {"stringValue": "newer"},
                                "createdAt": {"timestampValue": "2026-02-01T00:00:00Z"},
                                "whatsappConfirmed": {"booleanValue": False}}},
                ]
            }

    class DummyClient:
        async def get(self, url, params):
            return FakeResponse()

    rows = asyncio.run(posts._query_all_escorts(
        DummyClient(), order_field="createdAt", whatsapp_confirmed=True
    ))
    assert [row["slug"] for row in rows] == ["older"]
