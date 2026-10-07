"""Pure moderation logic."""
import re
import difflib
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

MIN_TEXT_LENGTH_EXACT = 15
MIN_TEXT_LENGTH_SIMILAR = 25
# Reworded copies of the same ad score 0.79-0.91 on real group messages; different
# members' texts never came above 0.6.
AD_SIMILARITY = 0.75
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+|\b(?:t\.me|telegram\.me|wa\.me)/[^\s<>\"']+",re.I)
PHONE_CANDIDATE_RE = re.compile(r"(?:\+|00)?\d[\d\s.\-()]{7,16}\d")
RO_MOBILE_RE = re.compile(r"^(?:0040|40)?0?(7\d{8})$")

@dataclass
class Fingerprint:
    text: str=""
    urls: set=field(default_factory=set)
    phones: set=field(default_factory=set)
    media: set=field(default_factory=set)
    def is_empty(self): return not (self.text or self.urls or self.phones or self.media)

def normalize_text(text):
    # NFKC folds "fancy font" letters (𝐃𝐢𝐬𝐩𝐨𝐧𝐢𝐛𝐢𝐥𝐚) back to plain ones, so a font swap is still the same text.
    text=unicodedata.normalize("NFKC",text or "").lower()
    text=URL_RE.sub(" ",text)
    text=re.sub(r"[@#]\w+"," ",text)
    text=re.sub(r"[^\w\s]","",text,flags=re.UNICODE)
    return re.sub(r"\s+"," ",text).strip()

def normalize_url(url):
    url=url.strip().rstrip(").,!?;:")
    if not re.match(r"^[a-z][a-z0-9+.-]*://",url,re.I): url="https://"+url
    parts=urlsplit(url); host=parts.netloc.lower()
    if host.startswith("www."): host=host[4:]
    if host=="telegram.me": host="t.me"
    path=parts.path.rstrip("/")
    if host in ("t.me","wa.me"): path=path.lower()
    query=urlencode([(k,v) for k,v in parse_qsl(parts.query) if not k.lower().startswith("utm_")])
    return urlunsplit(("",host,path,query,"")).lstrip("/")

def extract_urls(text,entity_urls=()):
    found=set(entity_urls)|set(URL_RE.findall(text or ""))
    return {normalize_url(u) for u in found if u.strip()}

def is_approape_url(url):
    normalized=normalize_url(url)
    host=normalized.split("/",1)[0].split("?",1)[0].split(":",1)[0].lower()
    return host=="approape.ro" or host.endswith(".approape.ro")

def links_to_approape(urls): return any(is_approape_url(url) for url in urls)
def has_external_link(urls): return any(not is_approape_url(url) for url in urls)

def normalize_phone(raw):
    digits=re.sub(r"\D","",raw); m=RO_MOBILE_RE.match(digits); return m.group(1) if m else None
def extract_phones(text,entity_phones=()):
    return {p for p in (normalize_phone(c) for c in list(entity_phones)+PHONE_CANDIDATE_RE.findall(text or "")) if p}
def build_fingerprint(text,entity_urls=(),entity_phones=(),media=()):
    return Fingerprint(normalize_text(text),extract_urls(text,entity_urls),extract_phones(text,entity_phones),{m for m in media if m})

# Selling words: a message with one of them is an ad even the first time, with no link.
# Matched with diacritics, spaces and punctuation removed, so "C A N A L  P R I V A T",
# "Scrie-mi aici" and "mesaj în privat" are caught.
AD_KEYWORDS = ("showweb", "sexting", "dickrating", "canalprivat", "grupprivat", "snapchat",
               "onlyfans", "fansly", "mesajinprivat", "scriemiinprivat", "scriemiaici", "haiinprivat")
# Ad layouts are built from custom emoji (Premium "stickers" inside the text); a chat message
# rarely carries this many.
AD_CUSTOM_EMOJI = 5

def compact_text(text):
    text=unicodedata.normalize("NFKC",text or "").lower()
    text="".join(c for c in unicodedata.normalize("NFD",text) if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]","",text)

def content_ad(text,custom_emoji=0):
    """An ad by its content: selling words, or a layout of custom emoji."""
    compact=compact_text(text)
    return custom_emoji>=AD_CUSTOM_EMOJI or any(k in compact for k in AD_KEYWORDS)

def same_ad_text(new,old):
    """Repeated text, on normalized strings: the same text, or a reworded copy of it."""
    if len(new)>=MIN_TEXT_LENGTH_EXACT and new==old: return True
    return (len(new)>=MIN_TEXT_LENGTH_SIMILAR and len(old)>=MIN_TEXT_LENGTH_SIMILAR
            and difflib.SequenceMatcher(None,new,old).ratio()>=AD_SIMILARITY)

def duplicate_reason(new,earlier):
    """Repeated media, link or phone. Repeated text is an ad instead (same_ad_text)."""
    if new.is_empty(): return None
    for old in earlier:
        if new.media & old.media: return "aceeași poză sau același clip"
        if new.urls & old.urls: return "același link"
        if new.phones & old.phones: return "același număr de telefon"
    return None

def violation_action(count_in_window):
    if count_in_window<=1: return "delete"
    if count_in_window<=3: return "warn"
    return "mute"
