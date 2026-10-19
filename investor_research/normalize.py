"""Normalization and validation of names, domains, e-mails and phones."""
from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

import phonenumbers
import tldextract

from . import config

_tld = tldextract.TLDExtract(suffix_list_urls=())  # bundled public-suffix snapshot, no network

LEGAL_FORMS = [
    "общество с ограниченной ответственностью", "публичное акционерное общество", "непубличное акционерное общество",
    "закрытое акционерное общество", "открытое акционерное общество", "акционерное общество",
    "товарищество с ограниченной ответственностью", "акционерный коммерческий банк", "коммерческий банк",
    "негосударственный пенсионный фонд", "управляющая компания", "инвестиционная компания",
    "limited liability partnership", "limited liability company", "private company", "public company",
    "ltd", "llc", "llp", "jsc", "plc", "inc", "ооо", "пао", "ао", "зао", "оао", "нао", "тоо", "ук", "ик",
    "акб", "кб", "нпф", "банк", "bank", "company", "co", "limited", "group", "the",
]
_form_re = re.compile(r"\b(" + "|".join(re.escape(f) for f in sorted(LEGAL_FORMS, key=len, reverse=True)) + r")\b")

_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i", "й": "i",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sh", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def clean_text(s) -> str | None:
    if s is None:
        return None
    s = html.unescape(str(s)).replace("\xa0", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def name_key(name: str) -> str:
    """Name key used for de-duplication: lower-case, legal forms and quotes
    removed, transliterated so that Latin and Cyrillic spellings converge."""
    s = (clean_text(name) or "").lower().replace("ё", "е")
    s = re.sub(r"[\"'«»“”„()\[\],.]", " ", s)
    s = _form_re.sub(" ", s)
    s = s.translate(_TRANSLIT)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


SOCIAL_HOSTS = ("vk.com", "telegram.me", "t.me", "facebook.com", "instagram.com", "ok.ru", "youtube.com",
                "twitter.com", "x.com", "linkedin.com", "rutube.ru", "dzen.ru", "tenchat.ru", "wa.me", "whatsapp.com",
                "apps.apple.com", "play.google.com", "rustore.ru", "appgallery.huawei.com")


def is_social(url: str) -> bool:
    host = urlsplit(url if "://" in url else "https://" + url).netloc.lower()
    return any(host == h or host.endswith("." + h) for h in SOCIAL_HOSTS)


def normalize_url(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip().strip("/").split()[0]
    if not raw or "." not in raw:
        return None
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    parts = urlsplit(raw)
    host = parts.netloc.lower()
    if not host or " " in host or is_social(host):
        return None
    return f"{parts.scheme.lower()}://{host}"


def registered_domain(url_or_host: str | None) -> str | None:
    if not url_or_host:
        return None
    host = urlsplit(url_or_host).netloc if "://" in url_or_host else url_or_host
    ext = _tld(host.lower())
    if not ext.domain or not ext.suffix:
        return None
    return f"{ext.domain}.{ext.suffix}"


EMAIL_RE = re.compile(r"(?<![\w.+-])([a-z0-9][a-z0-9._%+-]{0,63}@[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,24})(?![\w-])", re.I)
_BAD_EMAIL_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")


def extract_emails(text: str) -> list[str]:
    found = []
    for m in EMAIL_RE.finditer(text or ""):
        e = m.group(1).lower().strip(".")
        if e.endswith(_BAD_EMAIL_SUFFIX) or "example" in e or e.startswith(("noreply", "no-reply")):
            continue
        if "sentry" in e or "wixpress" in e or e.count("@") != 1:
            continue
        if e not in found:
            found.append(e)
    return found


def valid_email(e: str) -> bool:
    return bool(EMAIL_RE.fullmatch(e or ""))


def email_domain(e: str) -> str:
    return e.rsplit("@", 1)[-1].lower()


def is_freemail(e: str) -> bool:
    return email_domain(e) in config.FREEMAIL_DOMAINS


def is_role_address(e: str) -> bool:
    local = e.split("@", 1)[0].lower()
    base = re.split(r"[._-]", local)[0]
    return local in config.ROLE_LOCALPARTS or base in config.ROLE_LOCALPARTS


def normalize_phones(raw: str | None, country_iso: str) -> list[str]:
    """Return E.164 phones parsed from free text (several numbers allowed)."""
    if not raw:
        return []
    out = []
    try:
        matches = list(phonenumbers.PhoneNumberMatcher(str(raw), country_iso, leniency=phonenumbers.Leniency.VALID))
    except Exception:
        matches = []
    for m in matches:
        num = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
        if num not in out:
            out.append(num)
    if not out:
        # Registry fields like "(495) 967-09-13" or "8 (800) 775-88-88"
        for chunk in re.split(r"[;,/]| доб| ext", str(raw)):
            digits = re.sub(r"\D", "", chunk)
            if len(digits) < 7:
                continue
            try:
                n = phonenumbers.parse(chunk, country_iso)
                if phonenumbers.is_valid_number(n):
                    e164 = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
                    if e164 not in out:
                        out.append(e164)
            except phonenumbers.NumberParseException:
                continue
    return out


CITY_PATTERNS = [
    (r"\bг\.?\s*москва|\bгород москва|\bмосква\b", "Москва"),
    (r"санкт-петербург", "Санкт-Петербург"),
    (r"екатеринбург", "Екатеринбург"), (r"новосибирск", "Новосибирск"), (r"казань", "Казань"),
    (r"нижний новгород", "Нижний Новгород"), (r"самара", "Самара"), (r"ростов-на-дону", "Ростов-на-Дону"),
    (r"краснодар", "Краснодар"), (r"пермь", "Пермь"), (r"уфа\b", "Уфа"), (r"челябинск", "Челябинск"),
    (r"тюмень", "Тюмень"), (r"красноярск", "Красноярск"), (r"омск", "Омск"), (r"воронеж", "Воронеж"),
    (r"саратов", "Саратов"), (r"иркутск", "Иркутск"), (r"владивосток", "Владивосток"), (r"хабаровск", "Хабаровск"),
    (r"калининград", "Калининград"), (r"ярославл", "Ярославль"), (r"томск", "Томск"), (r"барнаул", "Барнаул"),
    (r"ижевск", "Ижевск"), (r"сочи\b", "Сочи"), (r"благовещенск", "Благовещенск"), (r"набережные челны", "Набережные Челны"),
    (r"astana|астана", "Астана"), (r"almaty|алматы", "Алматы"),
]


def city_from_address(address: str | None) -> str | None:
    if not address:
        return None
    a = address.lower()
    for pat, city in CITY_PATTERNS:
        if re.search(pat, a):
            return city
    m = re.search(r"\bг(?:ород|\.)\s*([А-ЯЁ][а-яё-]+)", address.title())
    return m.group(1) if m else None
