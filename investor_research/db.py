"""SQLite schema and data-access helpers.

Every material fact is linked to a row in `sources` (where it came from)
and contacts carry their own `source_url` and verification status.
"""
from __future__ import annotations

import functools
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS companies (
    company_id          INTEGER PRIMARY KEY,
    legal_name          TEXT NOT NULL,
    brand_name          TEXT,
    name_key            TEXT NOT NULL,            -- normalized name for dedup
    country             TEXT NOT NULL,
    city                TEXT,
    website             TEXT,
    website_status      TEXT,                     -- ok / redirect / unreachable / robots_disallow / NULL
    category            TEXT,
    priority_group      TEXT,                     -- A/B/C/D per task brief
    description         TEXT,
    aum                 TEXT,
    own_capital         TEXT,
    investment_focus    TEXT,
    instruments         TEXT,
    has_trading_desk    INTEGER,                  -- 1 = evidence found, NULL = unknown
    allocates_external  INTEGER,                  -- 1 = evidence found, NULL = unknown
    head_name           TEXT,
    head_position       TEXT,
    regulatory_status   TEXT,
    partnership_score   REAL,
    confidence_score    REAL,
    completeness_score  REAL,
    score_breakdown     TEXT,                     -- JSON
    relevance_reason    TEXT,
    tier                TEXT,
    excluded_reason     TEXT,                     -- not NULL -> filtered out of exports
    first_seen_at       TEXT NOT NULL,
    last_verified_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_companies_name_key ON companies(name_key, country);

CREATE TABLE IF NOT EXISTS company_identifiers (
    company_id  INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    id_type     TEXT NOT NULL,                    -- inn / ogrn / domain / naufor_id / afsa_licence / ...
    id_value    TEXT NOT NULL,
    PRIMARY KEY (id_type, id_value)
);

CREATE TABLE IF NOT EXISTS licenses (
    license_id   INTEGER PRIMARY KEY,
    company_id   INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    regulator    TEXT NOT NULL,
    license_type TEXT NOT NULL,
    number       TEXT,
    status       TEXT,
    issued_at    TEXT,
    source_url   TEXT NOT NULL,
    UNIQUE (company_id, regulator, license_type, number)
);

CREATE TABLE IF NOT EXISTS contacts (
    contact_id              INTEGER PRIMARY KEY,
    company_id              INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    contact_type            TEXT NOT NULL,        -- email / phone / website / contact_page / profile
    contact_value           TEXT NOT NULL,
    department              TEXT,
    public_contact_name     TEXT,
    public_contact_position TEXT,
    source_url              TEXT NOT NULL,
    source_kind             TEXT NOT NULL,        -- regulator / sro / exchange / company_site / curated
    verification_status     TEXT NOT NULL,        -- confirmed / unverified / invalid
    verification_note       TEXT,
    is_role_address         INTEGER,
    verified_at             TEXT,
    UNIQUE (company_id, contact_type, contact_value)
);

CREATE TABLE IF NOT EXISTS sources (
    source_id          INTEGER PRIMARY KEY,
    company_id         INTEGER REFERENCES companies(company_id) ON DELETE CASCADE,
    source_url         TEXT NOT NULL,
    source_type        TEXT NOT NULL,             -- regulator_registry / sro_registry / company_site / ...
    publication_date   TEXT,
    accessed_at        TEXT NOT NULL,
    http_status        INTEGER,
    extracted_evidence TEXT,                      -- JSON: {signal: [snippets]} or registry fields
    UNIQUE (company_id, source_url, source_type)
);

CREATE TABLE IF NOT EXISTS partnership_opportunities (
    opportunity_id      INTEGER PRIMARY KEY,
    company_id          INTEGER NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    partnership_type    TEXT NOT NULL,
    rationale           TEXT,
    supporting_evidence TEXT,
    constraints         TEXT,
    priority            TEXT,
    basis               TEXT NOT NULL DEFAULT 'analytical_assumption',  -- fact / analytical_assumption
    UNIQUE (company_id, partnership_type)
);

CREATE TABLE IF NOT EXISTS collection_runs (
    run_id            INTEGER PRIMARY KEY,
    started_at        TEXT NOT NULL,
    completed_at      TEXT,
    stages            TEXT,
    sources_processed INTEGER DEFAULT 0,
    new_companies     INTEGER DEFAULT 0,
    updated_companies INTEGER DEFAULT 0,
    errors            INTEGER DEFAULT 0,
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS run_errors (
    error_id   INTEGER PRIMARY KEY,
    run_id     INTEGER REFERENCES collection_runs(run_id),
    stage      TEXT,
    url        TEXT,
    message    TEXT,
    at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_coverage (
    channel     TEXT PRIMARY KEY,                 -- discovery channel name
    country     TEXT,
    url         TEXT,
    status      TEXT,                             -- processed / blocked / unreachable / partial
    records     INTEGER,
    note        TEXT,
    checked_at  TEXT
);

CREATE TABLE IF NOT EXISTS discovery_candidates (
    domain        TEXT NOT NULL,                  -- registered domain of a linked organisation
    anchor        TEXT,
    found_on      TEXT NOT NULL,                  -- page URL where the link was found
    company_id    INTEGER,                        -- company whose site links to it
    PRIMARY KEY (domain, company_id)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    stage   TEXT NOT NULL,
    item    TEXT NOT NULL,
    done_at TEXT NOT NULL,
    PRIMARY KEY (stage, item)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _locked(fn):
    @functools.wraps(fn)
    def wrapper(self, *a, **kw):
        with self.lock:
            return fn(self, *a, **kw)
    return wrapper


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=60, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.run_id: int | None = None
        self.lock = threading.RLock()
        self.stats = {"new": 0, "updated": 0, "sources": 0, "errors": 0}

    # --- runs / errors / checkpoints --------------------------------------
    @_locked
    def start_run(self, stages: str) -> int:
        cur = self.conn.execute("INSERT INTO collection_runs(started_at, stages) VALUES (?, ?)", (now(), stages))
        self.conn.commit()
        self.run_id = cur.lastrowid
        return self.run_id

    @_locked
    def finish_run(self, notes: str = "") -> None:
        self.conn.execute(
            "UPDATE collection_runs SET completed_at=?, sources_processed=?, new_companies=?,"
            " updated_companies=?, errors=?, notes=? WHERE run_id=?",
            (now(), self.stats["sources"], self.stats["new"], self.stats["updated"], self.stats["errors"], notes,
             self.run_id),
        )
        self.conn.commit()

    @_locked
    def error(self, stage: str, url: str | None, message: str) -> None:
        self.stats["errors"] += 1
        self.conn.execute("INSERT INTO run_errors(run_id, stage, url, message, at) VALUES (?,?,?,?,?)",
                          (self.run_id, stage, url, str(message)[:1000], now()))
        self.conn.commit()

    @_locked
    def done(self, stage: str, item: str) -> bool:
        return self.conn.execute("SELECT 1 FROM checkpoints WHERE stage=? AND item=?", (stage, item)).fetchone() is not None

    @_locked
    def mark_done(self, stage: str, item: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO checkpoints VALUES (?,?,?)", (stage, item, now()))
        self.conn.commit()

    @_locked
    def coverage(self, channel: str, country: str, url: str, status: str, records: int, note: str = "") -> None:
        self.conn.execute("INSERT OR REPLACE INTO source_coverage VALUES (?,?,?,?,?,?,?)",
                          (channel, country, url, status, records, note, now()))
        self.conn.commit()

    # --- companies ---------------------------------------------------------
    @_locked
    def find_company(self, identifiers: dict[str, str], name_key: str, country: str) -> int | None:
        for id_type, value in identifiers.items():
            if not value:
                continue
            row = self.conn.execute("SELECT company_id FROM company_identifiers WHERE id_type=? AND id_value=?",
                                    (id_type, str(value))).fetchone()
            if row:
                return row[0]
        row = self.conn.execute("SELECT company_id FROM companies WHERE name_key=? AND country=?",
                                (name_key, country)).fetchone()
        return row[0] if row else None

    @_locked
    def upsert_company(self, *, legal_name: str, name_key: str, country: str, identifiers: dict[str, str],
                       **fields) -> int:
        """Insert or merge a company. Existing non-NULL values are kept unless the
        new value comes from a more authoritative field set (caller decides via
        `overwrite`)."""
        overwrite = fields.pop("overwrite", False)
        cid = self.find_company(identifiers, name_key, country)
        fields = {k: v for k, v in fields.items() if v not in (None, "")}
        if cid is None:
            cols = ["legal_name", "name_key", "country", "first_seen_at", *fields.keys()]
            vals = [legal_name, name_key, country, now(), *fields.values()]
            cur = self.conn.execute(
                f"INSERT INTO companies({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
            cid = cur.lastrowid
            self.stats["new"] += 1
        else:
            row = self.conn.execute("SELECT * FROM companies WHERE company_id=?", (cid,)).fetchone()
            upd = {k: v for k, v in fields.items() if overwrite or row[k] in (None, "")}
            if upd:
                sets = ",".join(f"{k}=?" for k in upd)
                self.conn.execute(f"UPDATE companies SET {sets} WHERE company_id=?", (*upd.values(), cid))
                self.stats["updated"] += 1
        for id_type, value in identifiers.items():
            if value:
                self.conn.execute("INSERT OR IGNORE INTO company_identifiers VALUES (?,?,?)", (cid, id_type, str(value)))
        self.conn.commit()
        return cid

    @_locked
    def update_company(self, cid: int, **fields) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE companies SET {sets} WHERE company_id=?", (*fields.values(), cid))
        self.conn.commit()

    @_locked
    def add_identifier(self, cid: int, id_type: str, value: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO company_identifiers VALUES (?,?,?)", (cid, id_type, value))
        self.conn.commit()

    @_locked
    def add_license(self, cid: int, regulator: str, license_type: str, number: str | None, status: str | None,
                    issued_at: str | None, source_url: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO licenses(company_id, regulator, license_type, number, status, issued_at, source_url)"
            " VALUES (?,?,?,?,?,?,?)", (cid, regulator, license_type, number or "", status, issued_at, source_url))
        self.conn.commit()

    @_locked
    def add_contact(self, cid: int, contact_type: str, value: str, source_url: str, source_kind: str,
                    status: str, note: str | None = None, department: str | None = None,
                    name: str | None = None, position: str | None = None, is_role: bool | None = None) -> None:
        existing = self.conn.execute(
            "SELECT contact_id, verification_status FROM contacts WHERE company_id=? AND contact_type=? AND contact_value=?",
            (cid, contact_type, value)).fetchone()
        if existing:
            # Upgrade verification if a stronger source confirms the same value.
            if existing["verification_status"] != "confirmed" and status == "confirmed":
                self.conn.execute("UPDATE contacts SET verification_status=?, verification_note=?, source_url=?,"
                                  " source_kind=?, verified_at=? WHERE contact_id=?",
                                  (status, note, source_url, source_kind, now(), existing["contact_id"]))
                self.conn.commit()
            return
        self.conn.execute(
            "INSERT INTO contacts(company_id, contact_type, contact_value, department, public_contact_name,"
            " public_contact_position, source_url, source_kind, verification_status, verification_note,"
            " is_role_address, verified_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, contact_type, value, department, name, position, source_url, source_kind, status, note,
             None if is_role is None else int(is_role), now() if status == "confirmed" else None))
        self.conn.commit()

    @_locked
    def add_source(self, cid: int | None, url: str, source_type: str, evidence: dict | str | None = None,
                   http_status: int | None = None, publication_date: str | None = None) -> None:
        ev = json.dumps(evidence, ensure_ascii=False) if isinstance(evidence, (dict, list)) else evidence
        self.conn.execute(
            "INSERT INTO sources(company_id, source_url, source_type, publication_date, accessed_at, http_status,"
            " extracted_evidence) VALUES (?,?,?,?,?,?,?) ON CONFLICT(company_id, source_url, source_type) DO UPDATE SET"
            " accessed_at=excluded.accessed_at, http_status=excluded.http_status,"
            " extracted_evidence=excluded.extracted_evidence",
            (cid, url, source_type, publication_date, now(), http_status, ev))
        self.stats["sources"] += 1
        self.conn.commit()
