"""Pure moderation logic: what a message says, and whether it repeats an earlier one.

Nothing here talks to Telegram or the database, so it is unit-tested directly.
"""
import re
import difflib
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# Texts shorter than this are never called duplicates on their own: "ok" or
# "mersi" twice in six hours is conversation, not a repeated ad.
MIN_TEXT_LENGTH_EXACT = 15
MIN_TEXT_LENGTH_SIMILAR = 40

URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+|\b(?:t\.me|telegram\.me|wa\.me)/[^\s<>\"']+", re.I)
PHONE_CANDIDATE_RE = re.compile(r"(?:\+|00)?\d[\d\s.\-()]{7,16}\d")
# Romanian mobile numbers only: a bare run of digits is too often a price or a date.
RO_MOBILE_RE = re.compile(r"^(?:0040|40)?0?(7\d{8})$")


@dataclass
class Fingerprint:
    text: str = ""
    urls: set = field(default_factory=set)
    phones: set = field(default_factory=set)
    media: set = field(default_factory=set)

    def is_empty(self):
        return not (self.text or self.urls or self.phones or self.media)


def normalize_text(text):
    text = (text or "").lower()
    text = URL_RE.sub(" ", text)
    text = re.sub(r"[@#]\w+", " ", text)
    text = re.sub(r"[^\w\s]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(url):
    url = url.strip().rstrip(").,!?;:")
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host == "telegram.me":
        host = "t.me"
    path = parts.path.rstrip("/")
    # Telegram and WhatsApp handles are case-insensitive.
    if host in ("t.me", "wa.me"):
        path = path.lower()
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query) if not k.lower().startswith("utm_")])
    return urlunsplit(("", host, path, query, "")).lstrip("/")


def extract_urls(text, entity_urls=()):
    """URLs found by regex in the text plus those Telegram marked as entities
    (including links hidden behind words, which no regex can see)."""
    found = set(entity_urls) | set(URL_RE.findall(text or ""))
    return {normalize_url(u) for u in found if u.strip()}


def links_to_approape(urls):
    """True when any (normalized) URL points at approape.ro or one of its subdomains."""
    for url in urls:
        host = url.split("/", 1)[0].split("?", 1)[0].split(":", 1)[0]
        if host == "approape.ro" or host.endswith(".approape.ro"):
            return True
    return False


def normalize_phone(raw):
    digits = re.sub(r"\D", "", raw)
    m = RO_MOBILE_RE.match(digits)
    return m.group(1) if m else None


def extract_phones(text, entity_phones=()):
    candidates = list(entity_phones) + PHONE_CANDIDATE_RE.findall(text or "")
    return {p for p in (normalize_phone(c) for c in candidates) if p}


def build_fingerprint(text, entity_urls=(), entity_phones=(), media=()):
    return Fingerprint(
        text=normalize_text(text),
        urls=extract_urls(text, entity_urls),
        phones=extract_phones(text, entity_phones),
        media={m for m in media if m},
    )


def duplicate_reason(new, earlier, similarity_threshold):
    """The reason `new` repeats one of `earlier`, or None.

    Any shared photo, link or phone number is treated as the same promotion: in a
    group where people advertise, those are what an ad is about, while the
    wording around them changes from post to post.
    """
    if new.is_empty():
        return None
    for old in earlier:
        if new.media & old.media:
            return "aceeași poză sau același clip"
        if new.urls & old.urls:
            return "același link"
        if new.phones & old.phones:
            return "același număr de telefon"
        if len(new.text) >= MIN_TEXT_LENGTH_EXACT and new.text == old.text:
            return "același text"
        if len(new.text) >= MIN_TEXT_LENGTH_SIMILAR and len(old.text) >= MIN_TEXT_LENGTH_SIMILAR:
            if difflib.SequenceMatcher(None, new.text, old.text).ratio() >= similarity_threshold:
                return "text aproape identic"
    return None


def violation_action(count_in_window):
    """1st violation: delete only. 2nd and 3rd: delete + warning. 4th onwards: mute."""
    if count_in_window <= 1:
        return "delete"
    if count_in_window <= 3:
        return "warn"
    return "mute"
