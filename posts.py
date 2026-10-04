"""Scheduled posts in the group: profiles from approape.ro and questions for the members.

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
# Posts only from 10:00 to 01:00 local time (quiet 01:00–10:00), 2–3 a day.
# The window crosses midnight, so DAY_END_HOUR is smaller than DAY_START_HOUR.
DAY_START_HOUR = 10
DAY_END_HOUR = 1
MIN_GAP = timedelta(hours=3)
MAX_GAP = timedelta(hours=6)
NEW_PROFILE_DAYS = 30
PROFILE_REPEAT_DAYS = 30
QUESTION_REPEAT_DAYS = 14
# Questions marked "priority" (inviting people into the group) come back every few days.
PRIORITY_REPEAT_DAYS = 3
TRACKING = "utm_source=telegram&utm_medium=bot&utm_campaign=grup"

# Open questions are plain messages people answer in the group; polls are
# anonymous, so voting costs nothing and nobody's choice is on display.
QUESTIONS = [
    {"text": "Din ce oraș sunteți? Scrieți aici 👇 Vedem unde ar trebui să crească approape.ro."},
    {"text": "Ce oraș credeți că lipsește de pe approape.ro? 👇"},
    {"text": "Pentru fete: ce v-a adus cele mai multe mesaje — pozele, descrierea sau verificarea profilului? 👇"},
    {"text": "Ce sfat ai pentru cineva care își face primul profil pe approape.ro? 👇"},
    {"text": "Ce vă enervează cel mai tare când căutați un profil? Spuneți-ne, ca să reparăm. 👇"},
    {"text": "Ați găsit pe approape.ro ce căutați? Ce v-a lipsit? 👇"},
    {"poll": "Ce contează cel mai mult când alegi un profil?",
     "options": ["Poze reale / verificate", "Recenziile", "Răspunde repede", "Prețul"]},
    {"poll": "Din ce zonă ești?",
     "options": ["București", "Ardeal", "Moldova", "Banat / Crișana", "Dobrogea", "Oltenia / Muntenia"]},
    {"poll": "Cum preferi să iei legătura?",
     "options": ["WhatsApp", "Telegram", "Apel", "Mesaj pe site"]},
    {"poll": "Cât de des intri pe approape.ro?",
     "options": ["Zilnic", "De câteva ori pe săptămână", "Rar", "Acum aflu de el"]},
    {"poll": "Ce ți-ai dori să adăugăm pe approape.ro?",
     "options": ["Mai multe orașe", "Filtre mai bune", "Mai multe recenzii", "Aplicație pe telefon"]},
    {"poll": "Ai cont pe approape.ro?",
     "options": ["Da", "Nu încă", "Am încercat și n-a mers"]},
    # Appended, not inserted: questions already posted are remembered by index.
    {"priority": True,
     "text": "Ai fost mulțumit de o fată? Adaug-o în grup, ca să creăm împreună o comunitate interesată "
             "și interesantă 💪 Așa se strâng aici fete serioase, care nu dau țeapă, iar ceilalți știu la "
             "cine să meargă. O poți adăuga direct sau cu linkul tău de la @approape_guard_bot."},
    {"priority": True,
     "text": "Fetelor: adăugați în grup clienții mulțumiți, ca să creăm împreună o comunitate interesată "
             "și interesantă 💪 Îi puteți adăuga direct sau cu linkul vostru de la @approape_guard_bot."},
]

PHOTO_KEYS = ("url", "src", "photoUrl", "downloadURL")


@dataclass
class Profile:
    path: str      # /escorte/<slug> or /creatoare/<slug>
    name: str
    city: str
    photo: str
    created_at: datetime | None = None


# ------------------------------------------------------------
# When to post
# ------------------------------------------------------------
def _in_day_window(moment):
    hour = moment.astimezone(LOCAL_TZ).hour
    if DAY_START_HOUR < DAY_END_HOUR:
        return DAY_START_HOUR <= hour < DAY_END_HOUR
    return hour >= DAY_START_HOUR or hour < DAY_END_HOUR


def next_post_time(now, rng=random):
    """A random moment 3–6h from now, moved to the next morning if it falls at night."""
    candidate = now + MIN_GAP + (MAX_GAP - MIN_GAP) * rng.random()
    if _in_day_window(candidate):
        return candidate
    local = candidate.astimezone(LOCAL_TZ)
    morning = local.replace(hour=DAY_START_HOUR, minute=0, second=0, microsecond=0)
    if local.hour >= DAY_START_HOUR:
        morning += timedelta(days=1)
    return (morning + timedelta(hours=2) * rng.random()).astimezone(timezone.utc)


def due(now, next_at, last_post_at, last_human_at):
    """Time to post: the planned moment has come, it is daytime, and someone has
    written in the group since the bot's last post — never talk into silence."""
    if next_at is None or now < next_at or not _in_day_window(now):
        return False
    if last_human_at is None:
        return False
    return last_post_at is None or last_human_at > last_post_at


# ------------------------------------------------------------
# What to post
# ------------------------------------------------------------
def pick_question(recently_used, priority_used=frozenset(), rng=random):
    """A priority question not posted within PRIORITY_REPEAT_DAYS (priority_used) if there is
    one; otherwise a regular question not among the recently used ones, or any regular one."""
    due = [i for i, q in enumerate(QUESTIONS) if q.get("priority") and str(i) not in priority_used]
    if due:
        return rng.choice(due)
    regular = [i for i, q in enumerate(QUESTIONS) if not q.get("priority")]
    fresh = [i for i in regular if str(i) not in recently_used]
    return rng.choice(fresh or regular)


def pick_profile(new, top, recommended, recently_posted, now, rng=random):
    """(kind, profile): a new profile first, then last week's top, then a recommended one."""
    def fresh(profiles):
        return [p for p in profiles if p.path not in recently_posted and p.photo]

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
        "top": "🏆 Cel mai căutat profil săptămâna trecută",
        "recommended": "💫 Profil recomandat",
    }[kind]
    return f'{heading}\n<b>{name}</b>{where}\n<a href="{profile_url(profile)}">Vezi profilul</a>'


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
                "suspended", "disabled", "deletedAt", "awayMode", "awayUntil", "source"]


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


# Profiles store whatever the owner typed: @handle, a t.me link or a full URL
# (the site normalizes the same way in src/utils/telegram.ts).
TELEGRAM_HANDLE = re.compile(
    r"^(?:(?:https?://)?(?:www\.)?(?:t|telegram)\.me/)?@?([A-Za-z][A-Za-z0-9_]{3,31})/?(?:\?.*)?$",
    re.IGNORECASE)
HANDLE_FIELDS = ["telegramLink", "isHidden", "isDeleted", "banned", "suspended", "disabled", "deletedAt"]


def telegram_handle(raw):
    """The lowercased username in a profile's Telegram field, or None (invite links,
    phone numbers and anything else that does not name one account)."""
    match = TELEGRAM_HANDLE.match(str(raw or "").strip())
    return match.group(1).lower() if match else None


async def fetch_telegram_handles():
    """Usernames published on approape.ro profiles that are not hidden, deleted or banned."""
    query = {
        "from": [{"collectionId": "escorts"}],
        "select": {"fields": [{"fieldPath": f} for f in HANDLE_FIELDS]},
        "where": {"fieldFilter": {"field": {"fieldPath": "telegramLink"}, "op": "NOT_EQUAL",
                                  "value": {"stringValue": ""}}},
        "limit": 5000,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(FIRESTORE_QUERY, json={"structuredQuery": query})
        resp.raise_for_status()
    handles = set()
    for row in resp.json():
        fields = {k: _value(v) for k, v in (row.get("document") or {}).get("fields", {}).items()}
        if any(fields.get(k) for k in HANDLE_FIELDS[1:]):
            continue
        handle = telegram_handle(fields.get("telegramLink"))
        if handle:
            handles.add(handle)
    return handles


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
