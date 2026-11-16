"""Three daily profile spotlights from approape.ro, including WhatsApp-confirmed contacts.

The bot cannot wake itself on Render Free, so an external cron calls /tick every few
minutes; each call keeps the service awake and asks `due` whether it is time to post.
"""
import html
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx

SITE = "https://www.approape.ro"
LOCAL_TZ = ZoneInfo("Europe/Bucharest")
# Three profile spotlights per Bucharest calendar day; quiet hours remain 01:00–10:00.
DAY_START_HOUR = 10
DAY_END_HOUR = 1
POSTS_PER_DAY = 3
POST_HOURS = (11, 16, 21)
NEW_PROFILE_DAYS = 30
PROFILE_REPEAT_DAYS = 30
TRACKING = "utm_source=telegram&utm_medium=bot&utm_campaign=grup"

PHOTO_KEYS = ("url", "src", "photoUrl", "downloadURL")


@dataclass
class Profile:
    path: str      # /escorte/<slug> or /creatoare/<slug>
    name: str
    city: str
    photo: str
    created_at: datetime | None = None
    whatsapp_confirmed: bool = False


# ------------------------------------------------------------
# When to post
# ------------------------------------------------------------
def _in_day_window(moment):
    hour = moment.astimezone(LOCAL_TZ).hour
    if DAY_START_HOUR < DAY_END_HOUR:
        return DAY_START_HOUR <= hour < DAY_END_HOUR
    return hour >= DAY_START_HOUR or hour < DAY_END_HOUR


def next_post_time(now, posts_today=0, rng=random):
    """Return the next daily spotlight slot: approximately 11:00, 16:00 and 21:00 Bucharest time."""
    local = now.astimezone(LOCAL_TZ)
    if posts_today >= POSTS_PER_DAY:
        target = (local + timedelta(days=1)).replace(hour=POST_HOURS[0], minute=0, second=0, microsecond=0)
    else:
        target = local.replace(hour=POST_HOURS[posts_today], minute=0, second=0, microsecond=0)
        if target <= local:
            later = [h for h in POST_HOURS[posts_today + 1:] if h > local.hour]
            if later:
                target = local.replace(hour=later[0], minute=0, second=0, microsecond=0)
            else:
                target = (local + timedelta(days=1)).replace(hour=POST_HOURS[0], minute=0, second=0, microsecond=0)
    target += timedelta(minutes=int(rng.random() * 21))
    return target.astimezone(timezone.utc)

def due(now, next_at, last_post_at, last_human_at=None):
    """Post at the scheduled slot regardless of chat activity: the profile feed is a daily product surface."""
    return next_at is not None and now >= next_at and _in_day_window(now)


# ------------------------------------------------------------
# What to post
# ------------------------------------------------------------
def pick_profile(new, top, recommended, recently_posted, now, rng=random, require_whatsapp=False):
    """Pick a fresh profile, optionally requiring its WhatsApp contact to be confirmed."""
    def fresh(profiles):
        return [p for p in profiles if p.path not in recently_posted and p.photo
                and (not require_whatsapp or p.whatsapp_confirmed)]

    cutoff = now - timedelta(days=NEW_PROFILE_DAYS)
    new = [p for p in fresh(new) if p.created_at and p.created_at >= cutoff]
    if new:
        return "new", new[0]
    if top and fresh([top]):
        return "top", top
    candidates = fresh(recommended)[:10]
    if candidates:
        return "recommended", rng.choice(candidates)
    return None, None


def profile_url(profile):
    return f"{SITE}{profile.path}?{TRACKING}"


def profile_caption(kind, profile):
    name, city = html.escape(profile.name), html.escape(profile.city)
    where = f", {city}" if city else ""
    heading = {
        "new": "✨ Profil nou pe approape.ro",
        "top": "🏆 Profil popular săptămâna trecută",
        "recommended": "💫 Profil recomandat approape.ro",
    }[kind]
    contact = "\n📲 Contact WhatsApp confirmat" if profile.whatsapp_confirmed else ""
    return f'{heading}\n<b>{name}</b>{where}{contact}\n<a href="{profile_url(profile)}">Vezi profilul</a>'


# ------------------------------------------------------------
# Reading the site (public data only)
# ------------------------------------------------------------
def _value(field):
    """A Firestore REST value as plain Python."""
    if not field:
        return None
    kind, value = next(iter(field.items()))
    if kind == "arrayValue":
        return [_value(v) for v in value.get("values", [])]
    if kind == "mapValue":
        return {k: _value(v) for k, v in value.get("fields", {}).items()}
    if kind == "timestampValue":
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    if kind == "nullValue":
        return None
    return value


def first_photo(fields):
    for item in fields.get("photos") or []:
        if isinstance(item, str) and item.startswith("http"):
            return item
        if isinstance(item, dict):
            for key in PHOTO_KEYS:
                if str(item.get(key, "")).startswith("http"):
                    return item[key]
    for key in ("photoURL", "profilePhotoUrl", "photoUrl"):
        if str(fields.get(key) or "").startswith("http"):
            return fields[key]
    return ""


def profile_from_fields(fields):
    slug = fields.get("slug")
    if not slug:
        return None
    prefix = "creatoare" if fields.get("role") == "creatoare" else "escorte"
    return Profile(
        path=f"/{prefix}/{slug}",
        name=str(fields.get("displayName") or "").strip(),
        city=str(fields.get("currentCity") or fields.get("creatorCity") or "").strip(),
        photo=first_photo(fields),
        created_at=fields.get("createdAt"),
        whatsapp_confirmed=fields.get("whatsappConfirmed") is True,
    )


def is_listed_now(fields, now):
    """Live flags that can change after the sitemap was built."""
    if fields.get("isActive") is False:
        return False
    if any(fields.get(k) for k in ("isHidden", "isDeleted", "banned", "suspended", "disabled", "deletedAt")):
        return False
    if fields.get("awayMode"):
        until = fields.get("awayUntil")
        if not isinstance(until, datetime) or until > now:
            return False  # she is away: her contacts answer with a holiday note
    return True


def sitemap_paths(xml):
    return {m.group(1) for m in re.finditer(r"<loc>https://www\.approape\.ro(/[^<]+)</loc>", xml)}


QUERY_FIELDS = ["slug", "displayName", "role", "photos", "photoURL", "profilePhotoUrl", "photoUrl",
                "currentCity", "creatorCity", "createdAt", "isActive", "isHidden", "isDeleted", "banned",
                "suspended", "disabled", "deletedAt", "awayMode", "awayUntil", "source", "whatsappConfirmed"]


FIRESTORE_QUERY = ("https://firestore.googleapis.com/v1/projects/approape-bed86"
                   "/databases/(default)/documents:runQuery")


async def _query_escorts(client, limit, order_field=None, slug=None):
    """Public escorts documents (the collection is world-readable), as plain dicts."""
    query = {
        "from": [{"collectionId": "escorts"}],
        "select": {"fields": [{"fieldPath": f} for f in QUERY_FIELDS]},
        "limit": limit,
    }
    if order_field:
        query["orderBy"] = [{"field": {"fieldPath": order_field}, "direction": "DESCENDING"}]
    if slug:
        query["where"] = {"fieldFilter": {"field": {"fieldPath": "slug"}, "op": "EQUAL",
                                          "value": {"stringValue": slug}}}
    resp = await client.post(FIRESTORE_QUERY, json={"structuredQuery": query})
    resp.raise_for_status()
    return [{k: _value(v) for k, v in row["document"].get("fields", {}).items()}
            for row in resp.json() if row.get("document")]


async def fetch_site_profiles(now):
    """(new, top, recommended) — only profiles the site itself lists in its sitemaps,
    so the site's visibility rules are never copied here."""
    async with httpx.AsyncClient(timeout=15) as client:
        listed = set()
        for name in ("sitemap-escorte.xml", "sitemap-creatoare.xml"):
            resp = await client.get(f"{SITE}/{name}")
            resp.raise_for_status()
            listed |= sitemap_paths(resp.text)

        def usable(rows, claimed_only=False):
            profiles = []
            for fields in rows:
                if claimed_only and fields.get("source") != "claimed":
                    continue
                profile = profile_from_fields(fields)
                if profile and profile.path in listed and is_listed_now(fields, now):
                    profiles.append(profile)
            return profiles

        new = usable(await _query_escorts(client, 20, order_field="createdAt"))
        # Recommended: profiles their owners keep up to date, never imported ones.
        # Filtered here rather than in the query, which would need a composite index.
        recommended = usable(await _query_escorts(client, 60, order_field="updatedAt"), claimed_only=True)

        top = None
        try:
            resp = await client.get(f"{SITE}/api/top-weekly")
            resp.raise_for_status()
            path = ((resp.json().get("currentWeek") or {}).get("top") or {}).get("profilePath") or ""
            if path in listed:
                rows = await _query_escorts(client, 1, slug=path.rsplit("/", 1)[-1])
                top = next((p for p in usable(rows) if p.path == path), None)
        except Exception:
            top = None  # the ranking is optional; the other posts still work
        return new, top, recommended
