"""Member rosters of exchanges and financial associations in other CIS countries.

* Financial Association of Armenia (finarm.am): member cards with websites.
* Kyrgyz Stock Exchange (kse.kg): trading members with published contacts.

Only corporate contacts are kept: e-mails on free-mail domains and mobile
numbers in rosters frequently belong to individuals, so they are skipped.
"""
from __future__ import annotations

import html
import logging
import re

import phonenumbers

from ..db import DB
from ..fetch import Fetcher
from ..normalize import (clean_text, extract_emails, is_freemail, is_role_address, name_key, normalize_url,
                         registered_domain)

log = logging.getLogger("listings")

FINARM = "https://finarm.am/en/members"
KSE = "https://www.kse.kg/ru/Members"
ARM_BANKS = ("bank", "byblos", "ingo", "tel-cell", "telecom")


def _corporate_phones(raw: str, iso: str) -> list[str]:
    out = []
    for m in phonenumbers.PhoneNumberMatcher(raw, iso):
        if phonenumbers.number_type(m.number) == phonenumbers.PhoneNumberType.MOBILE:
            continue
        e = phonenumbers.format_number(m.number, phonenumbers.PhoneNumberFormat.E164)
        if e not in out:
            out.append(e)
    return out


def _flat(t: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", t, flags=re.S)
    return re.sub(r"(\|\s*)+", "|", html.unescape(re.sub(r"<[^>]+>", "|", t)))


def finarm(db: DB, fetcher: Fetcher, limit=None) -> int:
    p = fetcher.get(FINARM)
    if not p.ok:
        db.coverage("finarm_members", "Армения", FINARM, "unreachable", 0, str(p.error))
        return 0
    members = []
    for href, label in re.findall(r'<a[^>]+href="(https://finarm\.am/en/members/[a-z0-9-]+)"[^>]*>(.*?)</a>', p.text(), re.S):
        name = clean_text(re.sub(r"<[^>]+>", " ", label))
        name = clean_text(re.split(r"\s\+\d|Visit website", name or "")[0])
        if name and (href, name) not in members:
            members.append((href, name))
    n = 0
    for url, name in members[:limit] if limit else members:
        slug = url.rsplit("/", 1)[-1]
        if slug in ("tel-cell", "team-telecom"):
            continue  # telecoms: not financial partners
        d = fetcher.get(url)
        site = None
        emails = []
        if d.ok:
            t = d.text()
            ext = [h for h in re.findall(r'href="(https?://[^"]+)"', t)
                   if not re.search(r"finarm|facebook|instagram|linkedin|youtube|twitter|t\.me|google|munich", h)]
            site = normalize_url(ext[0]) if ext else None
            emails = [e for e in extract_emails(t) if not e.endswith("finarm.am")]
        bank = any(b in slug for b in ARM_BANKS)
        am = "asset-management" in slug or "funds" in slug
        cat, grp = (("Банк (инвестиционное подразделение)", "C") if bank else
                    ("Компания по управлению активами", "A") if am else
                    ("Инвестиционная компания / брокер", "B"))
        cid = db.upsert_company(legal_name=name, name_key=name_key(name), country="Армения", city="Ереван",
                                identifiers={"domain": registered_domain(site)}, website=site, category=cat,
                                priority_group=grp)
        db.add_source(cid, url, "association_registry", {"registry": "Financial Association of Armenia (finarm.am)"})
        dom = registered_domain(site)
        for e in emails:
            if dom and registered_domain(e.split("@")[1]) == dom:
                db.add_contact(cid, "email", e, url, "association", "confirmed",
                               "опубликован в карточке члена Финансовой ассоциации Армении; домен совпадает с сайтом",
                               is_role=is_role_address(e))
        n += 1
    db.coverage("finarm_members", "Армения", FINARM, "processed", n)
    return n


def kse(db: DB, fetcher: Fetcher, limit=None) -> int:
    p = fetcher.get(KSE)
    if not p.ok:
        db.coverage("kse_members", "Кыргызстан", KSE, "unreachable", 0, str(p.error))
        return 0
    full = _flat(p.text())
    start = full.find("Наименование")
    t = full[start:]
    exchange_phones = set(_corporate_phones(full[:start] + full[-3000:], "KG"))  # the exchange's own numbers
    # Each member starts with a legal form marker
    parts = re.split(r"\|(?=(?:ОАО|ЗАО|ОсОО|Брокерская компания|ООО)\b)", t)
    n = 0
    for part in parts[1:]:
        head = part.split("|")[0]
        name = clean_text(re.sub(r"\s+-\s*\(.*", "", head)).rstrip(" -")
        if not name or len(name) < 4:
            continue
        suspended = "приостановлен" in part[:300]
        if "МФК" in name or "микрофин" in part.lower()[:200]:
            continue
        site = normalize_url((re.search(r"(?:Сайт|Web сайт):?\|?\s*(www\.[^\s|]+|https?://[^\s|]+)", part) or [None, None])[1])
        emails = [e for e in extract_emails(part) if not is_freemail(e) and not e.endswith("kse.kg")]
        phones = [ph for ph in _corporate_phones(part, "KG") if ph not in exchange_phones]
        cat, grp = (("Негосударственный пенсионный фонд", "C") if "НПФ" in name else
                    ("Брокер / инвестиционная компания", "B"))
        cid = db.upsert_company(legal_name=name, name_key=name_key(name), country="Кыргызстан", city="Бишкек",
                                identifiers={"domain": registered_domain(site)}, website=site, category=cat,
                                priority_group=grp,
                                regulatory_status="Член ЗАО «Кыргызская фондовая биржа»" +
                                                  ("; доступ к торгам приостановлен" if suspended else ""))
        db.add_source(cid, KSE, "exchange_registry", {"registry": "Кыргызская фондовая биржа", "suspended": suspended})
        dom = registered_domain(site)
        for e in emails:
            st = "confirmed" if dom and registered_domain(e.split("@")[1]) == dom else "unverified"
            db.add_contact(cid, "email", e, KSE, "exchange", st, "опубликован в списке членов КФБ", is_role=is_role_address(e))
        for ph in phones:
            db.add_contact(cid, "phone", ph, KSE, "exchange", "confirmed", "стационарный номер из списка членов КФБ")
        n += 1
    db.coverage("kse_members", "Кыргызстан", KSE, "processed", n)
    return n


def ingest(db: DB, fetcher: Fetcher, limit: int | None = None) -> int:
    n = 0
    for fn in (finarm, kse, aix):
        try:
            n += fn(db, fetcher, limit)
        except Exception as ex:
            log.exception("listing %s", fn.__name__)
            db.error("listings", fn.__name__, repr(ex))
    db.coverage("bcse_members", "Беларусь", "https://www.bcse.by/ru/members", "skipped", 0,
                "условия сайта запрещают использование информации без письменного согласия; ручной поиск")
    db.coverage("kase_members", "Казахстан", "https://kase.kz/ru/members/", "blocked", 0,
                "WAF блокирует автоматический доступ; обход не выполнялся")
    db.coverage("ardfm_registry", "Казахстан", "https://www.ardfm.kz", "unreachable", 0, "сайт недоступен из среды сбора")
    db.coverage("moex_members", "Россия", "https://www.moex.com", "unreachable", 0, "сайт недоступен из среды сбора")
    db.coverage("uzse_members", "Узбекистан", "https://uzse.uz", "unreachable", 0, "сайт недоступен из среды сбора")
    db.coverage("moldse_members", "Молдова", "https://www.moldse.md", "unreachable", 0, "сайт недоступен из среды сбора")
    db.coverage("nlu_members", "Россия", "https://www.nlu.ru", "unreachable", 0, "сайт НЛУ недоступен из среды сбора")
    return n


AIX = "https://www.aixkz.com/en/pages/list-of-trading-members"
AIX_REGIONS = {"1": "AIFC", "2": "Kazakhstan", "3": "International"}
ADDRESS_COUNTRY = [("Kazakhstan", "Казахстан"), ("Russia", "Россия"), ("Moscow", "Россия"), ("UAE", "ОАЭ"),
                   ("Dubai", "ОАЭ"), ("Abu Dhabi", "ОАЭ"), ("Uzbekistan", "Узбекистан"), ("Tashkent", "Узбекистан"),
                   ("Kyrgyz", "Кыргызстан"), ("Armenia", "Армения"), ("Cyprus", "Кипр"), ("London", "Великобритания"),
                   ("United Kingdom", "Великобритания"), ("Hong Kong", "Гонконг"), ("China", "Китай")]


def aix(db: DB, fetcher: Fetcher, limit=None) -> int:
    """Astana International Exchange trading members (name, address, phone, website).
    E-mails on this page are hidden by Cloudflare e-mail protection and are deliberately
    not decoded; corporate e-mails are taken from the members' own websites instead."""
    from bs4 import BeautifulSoup
    p = fetcher.get(AIX)
    if not p.ok:
        db.coverage("aix_members", "Казахстан", AIX, "unreachable", 0, str(p.error))
        return 0
    soup = BeautifulSoup(p.text(), "lxml")
    n = 0
    for item in soup.select("div.memberships__item")[:limit] if limit else soup.select("div.memberships__item"):
        name_el = item.select_one(".memberships__item--name div")
        name = clean_text(name_el.get_text()) if name_el else None
        if not name:
            continue
        descr = [clean_text(d.get_text(" ")) for d in item.select(".memberships__item--descr > div")]
        descr = [d for d in descr if d]
        address = descr[0] if descr else ""
        region = AIX_REGIONS.get(item.get("data-region"), "")
        country = "Казахстан" if region in ("AIFC", "Kazakhstan") else \
            next((c for k, c in ADDRESS_COUNTRY if k.lower() in address.lower()), "Другая страна")
        site = None
        for a in item.select(".memberships__item--descr a[href]"):
            if "cdn-cgi" not in a["href"] and not a["href"].startswith("mailto"):
                site = normalize_url(a["href"])
        if not site:
            site = normalize_url(next((d for d in descr if re.match(r"^(https?://|www\.)", d)), None))
        bank = re.search(r"\bbank\b|банк", name, re.I)
        cid = db.upsert_company(
            legal_name=name, name_key=name_key(name), country=country,
            identifiers={"domain": registered_domain(site)}, website=site,
            city="Астана" if region == "AIFC" else ("Алматы" if "almaty" in address.lower() else None),
            category="Банк (участник торгов AIX)" if bank else "Брокер-дилер (участник торгов AIX)",
            priority_group="C" if bank else "B",
            regulatory_status=f"Участник торгов Astana International Exchange ({region})")
        db.add_source(cid, AIX, "exchange_registry", {"registry": "AIX trading members", "region": region,
                                                      "address": address})
        iso = {"Казахстан": "KZ", "Россия": "RU", "Узбекистан": "UZ"}.get(country, "KZ")
        for d in descr[1:]:
            if re.search(r"\d{3}", d) and "@" not in d and not re.match(r"^(https?://|www\.)", d):
                for ph in _corporate_phones(d, iso):
                    db.add_contact(cid, "phone", ph, AIX, "exchange", "confirmed", "опубликован в списке участников AIX")
        if site:
            db.add_contact(cid, "website", site, AIX, "exchange", "confirmed", "указан в списке участников AIX")
        n += 1
    db.coverage("aix_members", "Казахстан", AIX, "processed", n,
                "email на странице скрыт защитой Cloudflare — не декодировался")
    return n
