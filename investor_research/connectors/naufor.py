"""NAUFOR (self-regulatory organisation) register of members.

Each member card publishes the official address, phone, e-mail and website
reported by the member to its SRO, plus licence types. This is an
independent second official source for Russian participants and is used to
cross-confirm contacts obtained from the Bank of Russia.
"""
from __future__ import annotations

import html
import logging
import re
from concurrent.futures import ThreadPoolExecutor

from ..db import DB
from ..fetch import Fetcher
from ..normalize import (city_from_address, clean_text, extract_emails, is_freemail, is_role_address, name_key,
                         normalize_phones, normalize_url, registered_domain, valid_email)

log = logging.getLogger("naufor")
BASE = "https://naufor.ru"
REGISTER = BASE + "/tree.asp?n=13092"
SKIP_TYPES = ("регистратор", "специализированн", "депозитар")


def _flat(t: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>|<!--.*?-->", " ", t, flags=re.S)
    t = re.sub(r"<[^>]+>", "|", t)
    t = html.unescape(t).replace("\xa0", " ")
    return re.sub(r"(\|\s*)+", "|", t)


def _between(text: str, start: str, end: str) -> str | None:
    m = re.search(re.escape(start) + r"(.*?)" + re.escape(end), text, re.S)
    if not m:
        return None
    v = clean_text(m.group(1).replace("|", " "))
    return None if not v or v in ("-", "--") else v


def parse_card(raw: str) -> dict:
    t = _flat(raw)
    full = re.search(r"Фирменное наименование организации на русском языке\|П\|олное\|([^|]+)", t)
    short = re.search(r"Фирменное наименование организации на русском языке\|П\|олное\|[^|]+\|С\|окращенное\|([^|]+)", t)
    lic_block = _between(t, "сведения о лицензиях", "* * *") or ""
    licenses = [clean_text(x) for x in re.findall(r"(По [^|]{5,160})", t)]
    def field(label: str) -> str | None:
        m = re.search(re.escape(label) + r"[^|]*\|([^|]*)", t)
        if not m:
            return None
        v = clean_text(m.group(1))
        return None if not v or v in ("-", "--") or re.match(r"^\d\.\d+ ", v) else v

    return {
        "full_name": clean_text(full.group(1)) if full else None,
        "short_name": clean_text(short.group(1)) if short else None,
        "address": field("1.5 Адрес юридического лица"),
        "phone": field("1.6 Номер телефона"),
        "email": field("1.8 Адрес электронной почты"),
        "site": field("1.9 Адрес официального сайта"),
        "inn": (re.search(r"3\.1 Идентификационный номер налогоплательщика\|\s*(\d{10,12})", t) or [None, None])[1],
        "licenses": [l for l in licenses if l] or ([lic_block] if lic_block else []),
    }


def ingest(db: DB, fetcher: Fetcher, limit: int | None = None) -> int:
    page = fetcher.get(REGISTER)
    if not page.ok:
        db.error("naufor", REGISTER, page.error or page.status)
        db.coverage("naufor_register", "Россия", REGISTER, "unreachable", 0)
        return 0
    raw = page.text("cp1251")
    members = []
    seen = set()
    for href, name in re.findall(r'<a[^>]+href="(/compcard\.asp\?compid=\d+)"[^>]*>([^<]{2,200})</a>', raw):
        if href in seen:
            continue
        seen.add(href)
        members.append((BASE + href, clean_text(name)))
    if limit:
        members = members[:limit]
    log.info("NAUFOR: %d member cards", len(members))

    def get(m):
        try:
            return m, fetcher.get(m[0])
        except Exception as ex:
            return m, ex

    n = 0
    with ThreadPoolExecutor(max_workers=2) as pool:
        for (url, list_name), p in pool.map(get, members):
            if isinstance(p, Exception) or not p.ok:
                db.error("naufor_card", url, repr(p) if isinstance(p, Exception) else (p.error or p.status))
                continue
            try:
                if _store(db, url, list_name, parse_card(p.text("cp1251"))):
                    n += 1
            except Exception as ex:
                log.exception("naufor store")
                db.error("naufor_store", url, repr(ex))
    db.coverage("naufor_register", "Россия", REGISTER, "processed", n, f"{len(members)} member cards")
    return n


def _store(db: DB, url: str, list_name: str, c: dict) -> bool:
    legal = c["full_name"] or list_name
    lic_text = " ".join(c["licenses"]).lower()
    if c["licenses"] and all(any(s in l.lower() for s in SKIP_TYPES) for l in c["licenses"]):
        return False  # pure infrastructure (registrars, depositories) — not a capital partner
    site = normalize_url(c["site"])
    domain = registered_domain(site)
    nk = name_key(legal)
    existing = db.find_company({"inn": c["inn"], "domain": domain}, nk, "Россия")
    if existing is None:
        # Not in Bank of Russia lists we ingested (e.g. investment advisers): new company
        if "инвестиционн" in lic_text and "советник" in lic_text:
            cat, grp = "Инвестиционный советник", "D"
        elif "управлению" in lic_text:
            cat, grp = "Компания по управлению активами", "A"
        elif "дилер" in lic_text:
            cat, grp = "Дилер / торговля на собственные средства", "B"
        elif "брокер" in lic_text:
            cat, grp = "Институциональный брокер", "B"
        else:
            cat, grp = "Член НАУФОР (профучастник)", "D"
    else:
        cat = grp = None
    cid = db.upsert_company(legal_name=legal, brand_name=c["short_name"], name_key=nk, country="Россия",
                            identifiers={"inn": c["inn"], "domain": domain, "naufor_card": url},
                            city=city_from_address(c["address"]), website=site, category=cat, priority_group=grp)
    db.add_source(cid, url, "sro_registry", {"registry": "НАУФОР", "licenses": c["licenses"], "address": c["address"]})
    for em in extract_emails(c["email"] or ""):
        if valid_email(em):
            db.add_contact(cid, "email", em, url, "sro",
                           "unverified" if is_freemail(em) else "confirmed",
                           "опубликован в реестре членов НАУФОР" + ("; бесплатный почтовый домен" if is_freemail(em) else ""),
                           is_role=is_role_address(em))
    for ph in normalize_phones(c["phone"], "RU"):
        db.add_contact(cid, "phone", ph, url, "sro", "confirmed", "опубликован в реестре членов НАУФОР")
    if site:
        db.add_contact(cid, "website", site, url, "sro", "confirmed", "указан в реестре членов НАУФОР")
    return True
