"""Pure moderation logic."""
import re
import difflib
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

MIN_TEXT_LENGTH_EXACT = 15
MIN_TEXT_LENGTH_SIMILAR = 40
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
    text=(text or "").lower()
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

def duplicate_reason(new,earlier,similarity_threshold):
    if new.is_empty(): return None
    for old in earlier:
        if new.media & old.media: return "aceeași poză sau același clip"
        if new.urls & old.urls: return "același link"
        if new.phones & old.phones: return "același număr de telefon"
        if len(new.text)>=MIN_TEXT_LENGTH_EXACT and new.text==old.text: return "același text"
        if len(new.text)>=MIN_TEXT_LENGTH_SIMILAR and len(old.text)>=MIN_TEXT_LENGTH_SIMILAR:
            if difflib.SequenceMatcher(None,new.text,old.text).ratio()>=similarity_threshold: return "text aproape identic"
    return None

def violation_action(count_in_window):
    if count_in_window<=1: return "delete"
    if count_in_window<=3: return "warn"
    return "mute"
