"""Curated seeds: organisations found by manual/web research (exchange member
lists, association rosters, conference partner lists, media rankings).

Each row must carry `discovery_url` — the public page where the organisation
was found. Seeds only provide the name and official website; contacts are
then extracted from the organisation's own website by the enrichment stage,
so nothing in this file is treated as a confirmed contact.
"""
from __future__ import annotations

import csv
import logging

from .. import config
from ..db import DB
from ..fetch import Fetcher
from ..normalize import clean_text, name_key, normalize_url, registered_domain

log = logging.getLogger("seeds")
FIELDS = ["legal_name", "brand_name", "country", "city", "website", "category", "priority_group",
          "discovery_channel", "discovery_url", "note"]


def ingest(db: DB, fetcher: Fetcher, limit: int | None = None) -> int:
    n = 0
    for path in sorted(config.SEEDS_DIR.glob("*.csv")):
        with open(path, encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        channels: dict[str, int] = {}
        for r in rows:
            name = clean_text(r.get("legal_name"))
            if not name or not r.get("discovery_url"):
                continue
            site = normalize_url(r.get("website"))
            domain = registered_domain(site)
            cid = db.upsert_company(
                legal_name=name, brand_name=clean_text(r.get("brand_name")), name_key=name_key(name),
                country=clean_text(r["country"]), identifiers={"domain": domain},
                city=clean_text(r.get("city")), website=site, category=clean_text(r.get("category")),
                priority_group=clean_text(r.get("priority_group")), description=clean_text(r.get("note")))
            db.add_source(cid, r["discovery_url"], "discovery_listing",
                          {"channel": r.get("discovery_channel"), "note": r.get("note")})
            ch = r.get("discovery_channel") or path.stem
            channels[ch] = channels.get(ch, 0) + 1
            n += 1
        for ch, cnt in channels.items():
            db.coverage(f"seed:{ch}", "", str(path.name), "processed", cnt)
    return n
