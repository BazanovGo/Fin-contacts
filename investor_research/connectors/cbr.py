"""Bank of Russia (cbr.ru) registries.

Discovery: official XLSX registers of licensed securities-market participants
(brokers, dealers, securities managers), the mutual-fund (PIF) register and
the non-state pension fund register.
Enrichment: the regulator's organisation card (`/finorg/foinfo/?ogrn=`)
which publishes official e-mail, phone, web resources, charter capital and
the full list of licences.
"""
from __future__ import annotations

import io
import logging
import re
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
from bs4 import BeautifulSoup

from .. import config
from ..db import DB
from ..fetch import Fetcher
from ..normalize import (city_from_address, clean_text, extract_emails, is_freemail, is_role_address, name_key,
                         normalize_phones, normalize_url, registered_domain, valid_email)

log = logging.getLogger("cbr")
BASE = "https://www.cbr.ru"
LISTS = {
    "broker": "/vfs/finmarkets/files/supervision/list_brokers.xlsx",
    "dealer": "/vfs/finmarkets/files/supervision/list_dealers.xlsx",
    "trust": "/vfs/finmarkets/files/supervision/list_trust.xlsx",
}
PIF_LIST = "/vfs/finmarkets/files/supervision/list_PIF.xlsx"
NPF_LIST = "/vfs/finmarkets/files/supervision/list_npf_Valid.xlsx"
CARD = "/finorg/foinfo/?ogrn={ogrn}"

LICENSE_LABEL = {"broker": "Брокерская деятельность", "dealer": "Дилерская деятельность",
                 "trust": "Деятельность по управлению ценными бумагами"}
ACTIVE_PIF = ("Сформирован", "Формируется", "Согласован", "Зарегистрирован")
ALT_PIF_CATEGORIES = {"Хедж", "Товарного рынка", "Фонд рыночных финансовых инструментов", "Фонд финансовых инструментов",
                      "Фондов", "Смешанных инвестиций"}


def _read_xlsx(content: bytes, header_marker: str) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        raw = pd.read_excel(io.BytesIO(content), header=None)
    hdr = next(i for i in range(15) if any(str(v).strip().startswith(header_marker) for v in raw.iloc[i].tolist()))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return pd.read_excel(io.BytesIO(content), header=hdr)


def _s(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = clean_text(v)
    if s and re.fullmatch(r"\d+\.0", s):
        s = s[:-2]
    return s


def discover(db: DB, fetcher: Fetcher) -> dict[str, dict]:
    """Return {ogrn: entity} collected from all registers (not yet in DB)."""
    entities: dict[str, dict] = {}

    def ent(ogrn: str) -> dict:
        return entities.setdefault(ogrn, {"ogrn": ogrn, "licenses": [], "pif": defaultdict(int), "kinds": set()})

    for kind, path in LISTS.items():
        url = BASE + path
        page = fetcher.get(url)
        if not page.ok:
            db.error("cbr_discover", url, page.error or page.status)
            db.coverage(f"cbr_{kind}", "Россия", url, "unreachable", 0, str(page.error))
            continue
        df = _read_xlsx(page.content, "Наименование организации")
        n = 0
        for _, r in df.iterrows():
            ogrn = _s(r.get("ОГРН"))
            if not ogrn or not ogrn.isdigit():
                continue
            if _s(r.get("Статус")) and "Действ" not in _s(r.get("Статус")):
                continue
            e = ent(ogrn)
            e.update(legal_name=_s(r["Наименование организации"]), inn=_s(r.get("ИНН")),
                     address=_s(r.get("Адрес юридического лица")), registry_phone=_s(r.get("Телефоны")))
            e["kinds"].add(kind)
            e["licenses"].append((LICENSE_LABEL[kind], _s(r.get("№ лицензии")), _s(r.get("Статус")),
                                  _s(r.get("Дата выдачи")), url))
            n += 1
        db.coverage(f"cbr_{kind}", "Россия", url, "processed", n)

    # Mutual funds -> management companies with active funds
    url = BASE + PIF_LIST
    page = fetcher.get(url)
    if page.ok:
        df = _read_xlsx(page.content, "№ п/п")
        cols = list(df.columns)
        uk_name_col = "Полное фирменное наименование"
        uk_ogrn_col = cols[cols.index(uk_name_col) + 1]
        qual_col = next((c for c in cols if str(c).startswith("Инвестиционные паи предназначены")), None)
        n = 0
        for _, r in df.iterrows():
            if _s(r.get("Статус")) not in ACTIVE_PIF:
                continue
            ogrn = _s(r.get(uk_ogrn_col))
            if not ogrn or not ogrn.isdigit():
                continue
            e = ent(ogrn)
            e.setdefault("legal_name", _s(r.get(uk_name_col)))
            e["kinds"].add("uk_pif")
            cat = _s(r.get("Категория")) or "?"
            e["pif"][cat] += 1
            if qual_col and _s(r.get(qual_col)):
                e["pif"]["__qualified"] += 1
            n += 1
        db.coverage("cbr_pif", "Россия", url, "processed", n, f"{n} active funds")
    else:
        db.error("cbr_discover", url, page.error or page.status)

    url = BASE + NPF_LIST
    page = fetcher.get(url)
    if page.ok:
        df = _read_xlsx(page.content, "№ п/п")
        n = 0
        for _, r in df.iterrows():
            ogrn = _s(r.get("ОГРН"))
            if not ogrn or not ogrn.isdigit():
                continue
            e = ent(ogrn)
            e.update(legal_name=_s(r.get("Полное наименование")), brand_name=_s(r.get("Краткое наименование")),
                     inn=_s(r.get("ИНН")), address=_s(r.get("Адрес юридического лица")),
                     registry_phone=_s(r.get("Телефон")), registry_site=_s(r.get("Адрес сайта")))
            e["kinds"].add("npf")
            e["licenses"].append(("Негосударственный пенсионный фонд", _s(r.get("Номер лицензии")), "Действующая",
                                  _s(r.get("Дата выдачи")), url))
            n += 1
        db.coverage("cbr_npf", "Россия", url, "processed", n)
    return entities


def parse_card(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    out: dict = {"fields": {}, "licenses": [], "sites": [], "emails": []}
    for item in soup.select("div.coinfo_item"):
        t = item.select_one(".coinfo_item_title")
        v = item.select_one(".coinfo_item_text")
        if not t or not v:
            continue
        title = clean_text(t.get_text(" "))
        if title == "Интернет-ресурсы":
            out["sites"] = [clean_text(a.get_text()) for a in v.find_all("a")] or \
                           [s for s in re.split(r"[\s;,]+", v.get_text(" ")) if "." in s]
        elif title == "Адрес электронной почты":
            out["emails"] = extract_emails(v.get_text(" "))
        out["fields"][title] = clean_text(v.get_text(" "))
    table = soup.select_one("table.table_organization")
    if table:
        for tr in table.find_all("tr")[1:]:
            tds = [clean_text(td.get_text(" ")) for td in tr.find_all("td")]
            if len(tds) >= 5 and tds[0]:
                out["licenses"].append({"activity": tds[0], "number": tds[1], "name": tds[2], "start": tds[3],
                                        "end": tds[4], "services": tds[5] if len(tds) > 5 else None})
    return out


def pick_main_site(sites: list[str], legal_name: str = "") -> str | None:
    """Choose the corporate site among the regulator's list of web resources:
    prefer a domain resembling the company name, avoid client-portal hosts."""
    words = [w for w in re.findall(r"[a-z0-9]{4,}", name_key_words(legal_name))]
    cands = []
    for s in sites:
        u = normalize_url(s)
        if not u:
            continue
        host = u.split("://", 1)[1]
        bad = any(host.startswith(p) for p in ("lk.", "my.", "online.", "client", "cabinet", "trade.", "app.", "ib."))
        label = (registered_domain(u) or host).split(".")[0]
        match = any(w[:5] in label or label[:5] in w for w in words)
        cands.append((bad, not match, host.count("."), u))
    if not cands:
        return None
    cands.sort()
    return cands[0][3]


def name_key_words(name: str) -> str:
    """Transliterated words of a name without legal-form words (for domain matching)."""
    from ..normalize import _TRANSLIT, _form_re
    s = (name or "").lower().replace("ё", "е")
    s = re.sub(r"[\"'«»“”„()\[\],.]", " ", s)
    return _form_re.sub(" ", s).translate(_TRANSLIT)


def category_for(kinds: set[str], licenses: list[dict], name: str) -> tuple[str, str]:
    acts = " ".join(l["activity"] or "" for l in licenses).lower()
    lname = name.lower()
    bank = "банковских операций" in acts or re.search(r"\bбанк\b|bank", lname)
    if "npf" in kinds or "пенсионн" in lname:
        return "Негосударственный пенсионный фонд", "C"
    if "страхован" in acts and not bank:
        return "Страховая компания (инвестиционная деятельность)", "C"
    if bank:
        return "Банк (инвестиционное подразделение / доверительное управление)", "C"
    uk = "uk_pif" in kinds or "управлению инвестиционными фондами" in acts
    trust = "trust" in kinds or "управлению ценными бумагами" in acts
    dealer = "dealer" in kinds or "дилерск" in acts
    broker = "broker" in kinds or "брокерск" in acts
    if uk and trust:
        return "Управляющая компания (ПИФ + доверительное управление)", "A"
    if uk:
        return "Управляющая компания инвестиционных фондов", "A"
    if trust:
        return "Компания по управлению активами (доверительный управляющий)", "A"
    if dealer and not broker:
        return "Дилер / торговля на собственные средства", "B"
    if dealer and broker:
        return "Инвестиционная компания (брокер-дилер)", "B"
    if broker:
        return "Институциональный брокер", "B"
    return "Профессиональный участник рынка ценных бумаг", "B"


def ingest(db: DB, fetcher: Fetcher, limit: int | None = None) -> int:
    entities = discover(db, fetcher)
    items = sorted(entities.values(), key=lambda e: e["ogrn"])
    if limit:
        items = items[:limit]
    log.info("CBR: %d entities to process", len(items))

    def fetch_card(e):
        url = BASE + CARD.format(ogrn=e["ogrn"])
        try:
            return e, url, fetcher.get(url)
        except Exception as ex:  # robots or network
            return e, url, ex

    processed = 0
    with ThreadPoolExecutor(max_workers=2) as pool:  # single host: politeness delay serializes anyway
        for e, url, page in pool.map(fetch_card, items):
            try:
                _store_entity(db, e, url, page)
                processed += 1
            except Exception as ex:
                log.exception("cbr store failed %s", e.get("ogrn"))
                db.error("cbr_ingest", url, repr(ex))
    db.coverage("cbr_cards", "Россия", BASE + "/finorg/", "processed", processed)
    return processed


def _store_entity(db: DB, e: dict, url: str, page) -> None:
    card = None
    if not isinstance(page, Exception) and page.ok:
        card = parse_card(page.text())
    elif isinstance(page, Exception):
        db.error("cbr_card", url, repr(page))
    else:
        db.error("cbr_card", url, page.error or f"HTTP {page.status}")
    f = card["fields"] if card else {}
    legal = f.get("Полное (фирменное) наименование") or e.get("legal_name")
    if not legal:
        return
    brand = f.get("Сокращенное (фирменное) наименование") or e.get("brand_name")
    address = f.get("Адрес в пределах места нахождения") or e.get("address")
    status_ufr = f.get("Статус УФР")
    licenses = card["licenses"] if card else []
    active_licenses = [l for l in licenses if not l.get("end")]
    category, group = category_for(e["kinds"], active_licenses or licenses, legal)
    site = pick_main_site(card["sites"] if card else [], legal or "") or normalize_url(e.get("registry_site"))
    capital = f.get("Уставный капитал")
    if capital:
        capital = "Уставный капитал: " + capital.split(" руб")[0] + " руб. (реестр ЦБ РФ)"
    pif = dict(e["pif"])
    alt_pif = {k: v for k, v in pif.items() if k in ALT_PIF_CATEGORIES}
    focus = None
    if pif:
        cats = ", ".join(f"{k}: {v}" for k, v in sorted(pif.items(), key=lambda kv: -kv[1]) if not k.startswith("__"))
        focus = f"Действующие ПИФ по категориям — {cats}"
    reg_status = "; ".join(sorted({f"{l['activity']} № {l['number']}" for l in active_licenses}))[:900] or None
    cid = db.upsert_company(
        legal_name=legal, brand_name=brand, name_key=name_key(legal), country="Россия",
        identifiers={"ogrn": e["ogrn"], "inn": f.get("ИНН") or e.get("inn"),
                     "domain": registered_domain(site)},
        city=city_from_address(address), website=site, category=category, priority_group=group,
        own_capital=capital, investment_focus=focus,
        regulatory_status=("Банк России: " + reg_status) if reg_status else None,
        last_verified_at=None, overwrite=True,
    )
    evidence = {"registry": "Банк России", "ogrn": e["ogrn"], "status": status_ufr, "address": address,
                "licenses_active": [l["activity"] for l in active_licenses],
                "pif_categories": pif or None, "alt_pif": alt_pif or None,
                "lists": sorted(e["kinds"])}
    db.add_source(cid, url if card else BASE + "/finorg/", "regulator_registry", evidence,
                  http_status=None if isinstance(page, Exception) else page.status)
    for (lt, num, st, issued, src) in e["licenses"]:
        db.add_license(cid, "Банк России", lt, num, st, issued, src)
    for l in licenses:
        db.add_license(cid, "Банк России", l["activity"], l["number"], "Прекращена" if l.get("end") else "Действующая",
                       l.get("start"), url)
    # Contacts published by the regulator are treated as confirmed official contacts.
    for em in (card["emails"] if card else []):
        if valid_email(em):
            note = "опубликован Банком России" + ("; бесплатный почтовый домен" if is_freemail(em) else "")
            db.add_contact(cid, "email", em, url, "regulator", "unverified" if is_freemail(em) else "confirmed",
                           note, is_role=is_role_address(em))
    phones_raw = f.get("Номер телефона") or e.get("registry_phone")
    for ph in normalize_phones(phones_raw, "RU"):
        db.add_contact(cid, "phone", ph, url if card else BASE + "/finorg/", "regulator", "confirmed",
                       "опубликован Банком России")
    if site:
        db.add_contact(cid, "website", site, url if card else BASE + "/finorg/", "regulator", "confirmed",
                       "указан в реестре Банка России")
