"""Quality control: cross-source de-duplication and contact validation."""
from __future__ import annotations

import difflib
import logging
from collections import Counter, defaultdict

from .db import DB
from .normalize import is_freemail, looks_personal_email, name_key, registered_domain, valid_email

log = logging.getLogger("quality")
CHILD_TABLES = ["contacts", "sources", "licenses", "partnership_opportunities", "company_identifiers"]


def _similar(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a in b or b in a:
        return min(len(a), len(b)) >= 4
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def merge(db: DB, keep: int, drop: int) -> None:
    with db.lock:
        conn = db.conn
        for t in CHILD_TABLES:
            # OR IGNORE keeps the surviving record when both have the same unique value
            conn.execute(f"UPDATE OR IGNORE {t} SET company_id=? WHERE company_id=?", (keep, drop))
            conn.execute(f"DELETE FROM {t} WHERE company_id=?", (drop,))
        k = conn.execute("SELECT * FROM companies WHERE company_id=?", (keep,)).fetchone()
        d = conn.execute("SELECT * FROM companies WHERE company_id=?", (drop,)).fetchone()
        fill = {col: d[col] for col in d.keys() if col not in ("company_id",) and k[col] in (None, "") and d[col] not in (None, "")}
        if fill:
            conn.execute(f"UPDATE companies SET {','.join(f'{c}=?' for c in fill)} WHERE company_id=?", (*fill.values(), keep))
        conn.execute("DELETE FROM companies WHERE company_id=?", (drop,))
        conn.commit()


def dedupe(db: DB) -> int:
    rows = db.conn.execute("SELECT company_id, name_key, country, website, (SELECT COUNT(*) FROM company_identifiers i"
                           " WHERE i.company_id=c.company_id AND i.id_type IN ('ogrn','inn')) AS has_reg"
                           " FROM companies c").fetchall()
    merged = 0
    # 1) same registered domain + similar name, same country
    by_domain = defaultdict(list)
    for r in rows:
        d = registered_domain(r["website"])
        if d:
            by_domain[(d, r["country"])].append(r)
    gone = set()
    for (_, _), grp in by_domain.items():
        if len(grp) < 2:
            continue
        grp = sorted(grp, key=lambda r: (-r["has_reg"], r["company_id"]))
        for other in grp[1:]:
            base = grp[0]
            if other["company_id"] in gone or base["company_id"] in gone:
                continue
            if other["has_reg"] and base["has_reg"]:
                continue  # two different registered legal entities sharing a group website
            if _similar(base["name_key"], other["name_key"]):
                merge(db, base["company_id"], other["company_id"])
                gone.add(other["company_id"])
                merged += 1
    # 2) identical name key in the same country
    by_name = defaultdict(list)
    for r in rows:
        if r["company_id"] not in gone and r["name_key"]:
            by_name[(r["name_key"], r["country"])].append(r)
    for grp in by_name.values():
        if len(grp) < 2:
            continue
        grp = sorted(grp, key=lambda r: (-r["has_reg"], r["company_id"]))
        for other in grp[1:]:
            if other["has_reg"] and grp[0]["has_reg"]:
                continue
            merge(db, grp[0]["company_id"], other["company_id"])
            gone.add(other["company_id"])
            merged += 1
    log.info("dedupe merged %d records", merged)
    return merged


def validate_contacts(db: DB) -> dict:
    stats = {"invalid_email": 0, "freemail": 0, "personal_removed": 0}
    with db.lock:
        for r in db.conn.execute("SELECT contact_id, contact_value FROM contacts WHERE contact_type='email'").fetchall():
            if looks_personal_email(r["contact_value"]):
                # Data minimisation: named employee addresses are personal data — not kept.
                db.conn.execute("DELETE FROM contacts WHERE contact_id=?", (r["contact_id"],))
                stats["personal_removed"] += 1
            elif not valid_email(r["contact_value"]):
                db.conn.execute("UPDATE contacts SET verification_status='invalid', verification_note='неверный синтаксис'"
                                " WHERE contact_id=?", (r["contact_id"],))
                stats["invalid_email"] += 1
            elif is_freemail(r["contact_value"]):
                db.conn.execute("UPDATE contacts SET verification_status='unverified' WHERE contact_id=? AND"
                                " verification_status='confirmed'", (r["contact_id"],))
                stats["freemail"] += 1
        db.conn.commit()
    if stats["personal_removed"]:
        db.coverage("privacy_filter", "", "contacts", "processed", stats["personal_removed"],
                    "именные адреса сотрудников удалены (минимизация персональных данных)")
    return stats


TLD_COUNTRY = {"ru": "Россия", "kz": "Казахстан", "by": "Беларусь", "uz": "Узбекистан", "am": "Армения",
               "az": "Азербайджан", "kg": "Кыргызстан", "tj": "Таджикистан", "md": "Молдова", "tm": "Туркменистан",
               "рф": "Россия"}


def promote_candidates(db: DB, min_referrers: int = 2) -> int:
    """Turn domains linked from >= `min_referrers` different known companies into
    new candidate organisations (verified later by website enrichment)."""
    known = {r[0] for r in db.conn.execute("SELECT id_value FROM company_identifiers WHERE id_type='domain'")}
    rows = db.conn.execute(
        "SELECT domain, COUNT(DISTINCT company_id) n, GROUP_CONCAT(anchor, ' || ') anchors, MIN(found_on) found_on,"
        " GROUP_CONCAT(DISTINCT company_id) refs FROM discovery_candidates GROUP BY domain HAVING n >= ?",
        (min_referrers,)).fetchall()
    from .enrich import _NOT_CANDIDATE
    blocked = lambda d: any(d == x or d.endswith("." + x) for x in _NOT_CANDIDATE)
    # Candidates promoted by an earlier run whose domain is now block-listed (media, ratings, ...)
    for cid, site in db.conn.execute("SELECT company_id, website FROM companies WHERE category LIKE 'Кандидат%'"
                                     " AND excluded_reason IS NULL").fetchall():
        if blocked(registered_domain(site) or ""):
            db.update_company(cid, excluded_reason="СМИ/рейтинговый ресурс, не организация-партнёр")
    added = 0
    for r in rows:
        if r["domain"] in known or blocked(r["domain"]):
            continue
        anchors = [a.strip() for a in (r["anchors"] or "").split("||")
                   if 3 <= len(a.strip()) <= 60 and len(a.split()) <= 6 and not a.strip().lower().startswith(("наш", "our"))]
        name = Counter(anchors).most_common(1)[0][0] if anchors else r["domain"]
        tld = r["domain"].rsplit(".", 1)[-1]
        ref_countries = Counter(c[0] for c in db.conn.execute(
            f"SELECT country FROM companies WHERE company_id IN ({r['refs']})"))
        country = TLD_COUNTRY.get(tld) or ref_countries.most_common(1)[0][0]
        cid = db.upsert_company(legal_name=name, name_key=name_key(name) or r["domain"], country=country,
                                identifiers={"domain": r["domain"]}, website=f"https://{r['domain']}",
                                category="Кандидат: обнаружен по перекрёстным ссылкам", priority_group="D")
        db.add_source(cid, r["found_on"], "crosslink_discovery",
                      {"referring_companies": r["refs"], "n_referrers": r["n"], "anchors": anchors[:5]})
        added += 1
    db.coverage("crosslinks", "", "discovery_candidates", "processed", added,
                f"{len(rows)} доменов со ссылками от ≥{min_referrers} организаций")
    return added


def provenance_check(db: DB) -> dict:
    """Re-open each confirmed e-mail/phone's source page from the HTTP cache and check
    that the value is really printed there (phones: last 7 digits)."""
    import html as _html
    import re as _re
    import sqlite3 as _sqlite3

    from . import config
    cache = _sqlite3.connect(config.CACHE_DB_PATH)
    pages: dict[str, str | None] = {}
    res = {"found": 0, "not_found": 0, "no_cached_page": 0}
    for r in db.conn.execute("SELECT contact_type, contact_value, source_url FROM contacts WHERE"
                             " verification_status='confirmed' AND contact_type IN ('email','phone')").fetchall():
        u = r["source_url"]
        if u not in pages:
            row = cache.execute("SELECT content FROM pages WHERE url=? OR final_url=? LIMIT 1", (u, u)).fetchone()
            raw = row[0] if row and row[0] else None
            if raw is None:
                pages[u] = None
            else:
                try:
                    pages[u] = _html.unescape(raw.decode("utf-8"))
                except UnicodeDecodeError:
                    pages[u] = _html.unescape(raw.decode("cp1251", errors="replace"))
        text = pages[u]
        if text is None:
            res["no_cached_page"] += 1
            continue
        if r["contact_type"] == "email":
            ok = r["contact_value"].lower() in text.lower()
        else:
            ok = r["contact_value"][-7:] in _re.sub(r"\D", "", text)
        res["found" if ok else "not_found"] += 1
    db.coverage("provenance_check", "", "contacts", "processed", res["found"],
                f"найдено на странице-источнике: {res['found']}; не найдено: {res['not_found']}; "
                f"страница не в кэше: {res['no_cached_page']}")
    return res
