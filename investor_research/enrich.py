"""Website enrichment: crawl a company's own site (home page + a few
contact/about pages, same registered domain only, robots.txt respected),
extract corporate e-mails, phones, description and evidence of investment
activity used for scoring."""
from __future__ import annotations

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlsplit

import phonenumbers
from bs4 import BeautifulSoup

from . import config
from .db import DB, now
from .fetch import Fetcher, RobotsDisallowed
from .normalize import (clean_text, email_domain, extract_emails, is_freemail, is_role_address, name_key,
                        registered_domain)

log = logging.getLogger("enrich")
_SIGNAL_RE = {k: re.compile("|".join(v), re.I) for k, v in config.SIGNALS.items()}
_PHONE_TEXT_RE = re.compile(r"(?:\+\s?\d{1,3}|\b8)[\s\-]?\(?\d{2,5}\)?[\s\-]?\d{1,4}[\s\-]\d{2}[\s\-]?\d{2}\b")


def page_text(soup: BeautifulSoup) -> str:
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    return clean_text(soup.get_text(" ")) or ""


def find_signals(text: str, limit: int = 3) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for sig, rx in _SIGNAL_RE.items():
        snippets = []
        for m in rx.finditer(text):
            s = clean_text(text[max(0, m.start() - 90): m.end() + 110])
            if s and s not in snippets:
                snippets.append(s)
            if len(snippets) >= limit:
                break
        if snippets:
            out[sig] = snippets
    return out


def extract_phones(soup: BeautifulSoup, text: str, iso: str) -> list[str]:
    raw = [a["href"][4:] for a in soup.select('a[href^="tel:"]')]
    raw += _PHONE_TEXT_RE.findall(text)
    out = []
    for r in raw:
        try:
            n = phonenumbers.parse(r.strip(), iso)
        except phonenumbers.NumberParseException:
            continue
        if phonenumbers.is_valid_number(n):
            e = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
            if e not in out:
                out.append(e)
    return out[:6]


def contact_links(soup: BeautifulSoup, base_url: str, domain: str) -> list[str]:
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        label = (a.get_text(" ") + " " + href).lower()
        if not any(h in label for h in config.CONTACT_LINK_HINTS):
            continue
        u = urljoin(base_url, href).split("#")[0]
        if registered_domain(u) != domain or re.search(r"\.(pdf|docx?|xlsx?|zip|jpg|png)$", u, re.I):
            continue
        if u not in links and u.rstrip("/") != base_url.rstrip("/"):
            links.append(u)
    # Contacts first, then about/disclosure pages.
    links.sort(key=lambda u: 0 if re.search(r"contact|kontakt|контакт", u, re.I) else 1)
    return links[: config.MAX_PAGES_PER_SITE - 1]


def _name_tokens(row) -> list[str]:
    toks = []
    for n in (row["brand_name"], row["legal_name"]):
        if not n:
            continue
        for w in re.findall(r"[A-Za-zА-Яа-яЁё0-9\-]{4,}", n):
            wl = w.lower()
            if wl not in ("общество", "ограниченной", "ответственностью", "акционерное", "компания", "управляющая",
                          "limited", "company", "partnership", "liability", "инвестиционная", "private", "банк"):
                toks.append(wl)
    return toks


def enrich_company(db: DB, fetcher: Fetcher, row) -> dict:
    cid, site = row["company_id"], row["website"]
    iso = config.COUNTRY_ISO.get(row["country"], "RU")
    result = {"company_id": cid, "status": None}
    try:
        home = fetcher.get(site)
    except RobotsDisallowed:
        db.update_company(cid, website_status="robots_disallow")
        db.error("enrich", site, "robots.txt disallows crawling")
        return result
    if not home.ok:
        status = "unreachable" if home.status == 0 else f"http_{home.status}"
        db.update_company(cid, website_status=status)
        db.error("enrich", site, home.error or f"HTTP {home.status}")
        return result
    if "html" not in home.content_type.lower():
        db.update_company(cid, website_status="non_html")
        return result
    domain = registered_domain(site)
    final_domain = registered_domain(home.final_url)
    status = "ok" if final_domain == domain else f"redirect:{final_domain}"
    own_domains = {d for d in (domain, final_domain) if d}

    # Was the website linked to this company by an official registry?
    with db.lock:
        site_official = db.conn.execute(
            "SELECT 1 FROM contacts WHERE company_id=? AND contact_type='website' AND source_kind IN ('regulator','sro')",
            (cid,)).fetchone() is not None

    pages = [(home.final_url, home)]
    soup = BeautifulSoup(home.text(), "lxml")
    for u in contact_links(soup, home.final_url, final_domain or domain):
        try:
            p = fetcher.get(u)
        except RobotsDisallowed:
            continue
        if p.ok and "html" in p.content_type.lower():
            pages.append((u, p))

    all_text = []
    name_hit = False
    tokens = _name_tokens(row)
    meta_desc = None
    for url, p in pages:
        s = BeautifulSoup(p.text(), "lxml")
        if meta_desc is None:
            m = s.find("meta", attrs={"name": re.compile("^description$", re.I)}) or \
                s.find("meta", attrs={"property": "og:description"})
            if m and m.get("content"):
                meta_desc = clean_text(m["content"])
        mailtos = [a["href"][7:].split("?")[0] for a in s.select('a[href^="mailto:"]')]
        txt = page_text(s)
        all_text.append(txt)
        low = txt.lower()
        if tokens and any(t in low for t in tokens):
            name_hit = True
        trusted = site_official or name_hit
        for em in extract_emails(" ".join(mailtos) + " " + txt):
            d = email_domain(em)
            if d in own_domains or registered_domain(d) in own_domains:
                st = "confirmed" if trusted else "unverified"
                note = "домен адреса совпадает с официальным сайтом" + ("" if trusted else "; принадлежность сайта не подтверждена")
            elif is_freemail(em):
                st, note = "unverified", "бесплатный почтовый домен на сайте компании"
            else:
                st, note = "unverified", f"домен {d} отличается от домена сайта"
            db.add_contact(cid, "email", em, url, "company_site", st, note, is_role=is_role_address(em))
        for ph in extract_phones(s, txt, iso):
            db.add_contact(cid, "phone", ph, url, "company_site", "confirmed" if trusted else "unverified",
                           "опубликован на официальном сайте" if trusted else "сайт не подтверждён реестром")
        for a in s.select('a[href*="linkedin.com/company"]')[:1]:
            db.add_contact(cid, "profile", a["href"].split("?")[0], url, "company_site",
                           "confirmed" if trusted else "unverified", "ссылка с официального сайта")
        _collect_candidates(db, cid, s, url, own_domains)
        if re.search(r"contact|kontakt|контакт", url, re.I):
            db.add_contact(cid, "contact_page", url, url, "company_site",
                           "confirmed" if trusted else "unverified", "страница контактов на сайте")
        sig = find_signals(txt)
        db.add_source(cid, url, "company_site", {"signals": sig} if sig else {"signals": {}}, http_status=p.status)

    if not site_official:
        # Website came from a curated listing: confirm it only if the site names the company.
        db.add_contact(cid, "website", site, home.final_url, "company_site",
                       "confirmed" if name_hit else "unverified",
                       "сайт доступен и содержит название организации" if name_hit
                       else "сайт доступен, но название организации на нём не найдено")
    text = " ".join(all_text)
    signals = find_signals(text, limit=4)
    upd = {"website_status": status, "last_verified_at": now()}
    if not row["description"] and meta_desc and len(meta_desc) > 30:
        upd["description"] = meta_desc[:600]
    if signals.get("prop_trading") or signals.get("derivatives") and "дилер" in (row["category"] or "").lower():
        upd["has_trading_desk"] = 1
    if signals.get("external_managers"):
        upd["allocates_external"] = 1
    instruments = [k for k in ("derivatives", "commodities", "quant") if k in signals]
    if instruments:
        upd["instruments"] = ", ".join({"derivatives": "деривативы/фьючерсы", "commodities": "товарные рынки",
                                        "quant": "алгоритмические/количественные стратегии"}[k] for k in instruments)
    aum = _find_aum(text)
    if aum and not row["aum"]:
        upd["aum"] = aum
    db.update_company(cid, **upd)
    result["status"] = status
    return result


_CANDIDATE_RE = re.compile(r"капитал|инвест|invest|capital|asset|fund|фонд|управля|trust|траст|брокер|broker|"
                           r"trading|трейдинг|wealth|holding|холдинг|partners|партнер", re.I)
_NOT_CANDIDATE = ("cbr.ru", "gov.ru", "gosuslugi.ru", "nalog.ru", "consultant.ru", "garant.ru", "moex.com",
                  "e-disclosure.ru", "disclosure.ru", "interfax.ru", "naufor.ru", "spbexchange.ru", "yandex.ru",
                  "google.com", "apple.com", "1c.ru", "bitrix24.ru", "1c-bitrix.ru", "tilda.cc", "wordpress.org",
                  "rbc.ru", "kommersant.ru", "vedomosti.ru", "nrd.ru", "finuslugi.ru", "fincult.info", "mos.ru")


def _collect_candidates(db: DB, cid: int, soup: BeautifulSoup, page_url: str, own_domains: set) -> None:
    """Outbound links to other financial organisations: a discovery channel for
    companies not present in registries (partners, group members, funds)."""
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.startswith("http"):
            continue
        d = registered_domain(href)
        if not d or d in own_domains or d.endswith(".gov.ru") or any(d == x or d.endswith("." + x) for x in _NOT_CANDIDATE):
            continue
        anchor = clean_text(a.get_text(" ")) or ""
        if not (_CANDIDATE_RE.search(d) or _CANDIDATE_RE.search(anchor)):
            continue
        with db.lock:
            db.conn.execute("INSERT OR IGNORE INTO discovery_candidates VALUES (?,?,?,?)", (d, anchor[:200], page_url, cid))
            db.conn.commit()


_AUM_RE = re.compile(
    r"((?:активы|активов|средства|средств)\s+(?:под управлением|в управлении)[^.]{0,60}?"
    r"(\d[\d\s,.]*\s*(?:млрд|млн|трлн|billion|million|bn|mln)\.?\s*(?:руб\w*|долл\w*|\$|USD|RUB|тенге|KZT)?))"
    r"|((?:AUM|assets under management)[^.]{0,40}?(\$?\s?\d[\d\s,.]*\s*(?:billion|million|bn|mln|млрд|млн)\.?\s*(?:USD|\$|RUB|KZT)?))",
    re.I)


def _find_aum(text: str) -> str | None:
    m = _AUM_RE.search(text)
    if not m:
        return None
    return clean_text((m.group(1) or m.group(3)))[:200] + " (с сайта компании)"


def run(db: DB, fetcher: Fetcher, limit: int | None = None, refresh: bool = False, workers: int = config.WORKERS) -> int:
    q = "SELECT * FROM companies WHERE website IS NOT NULL AND excluded_reason IS NULL"
    rows = db.conn.execute(q).fetchall()
    todo = [r for r in rows if refresh or not db.done("enrich", str(r["company_id"]))]
    if limit:
        todo = todo[:limit]
    log.info("enrich: %d companies", len(todo))
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(enrich_company, db, fetcher, r): r for r in todo}
        for f in as_completed(futs):
            r = futs[f]
            try:
                f.result()
                db.mark_done("enrich", str(r["company_id"]))
                done += 1
            except Exception as ex:
                log.exception("enrich failed %s", r["website"])
                db.error("enrich", r["website"], repr(ex))
            if done % 50 == 0:
                log.info("enrich progress %d/%d", done, len(todo))
    return done
