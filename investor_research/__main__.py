"""Command-line entry point.

    python -m investor_research run                 # full pipeline (resumable)
    python -m investor_research run --stages cbr,enrich --limit 30   # pilot
    python -m investor_research export              # re-export only
    python -m investor_research run --refresh       # re-crawl websites (incremental update)
"""
from __future__ import annotations

import argparse
import logging
import sys

from . import config, enrich, export, quality, scoring
from .connectors import afsa, cbr, listings, naufor, seeds
from .db import DB
from .fetch import Fetcher

STAGES = ["cbr", "naufor", "afsa", "listings", "seeds", "dedupe", "enrich", "crosslinks", "quality", "score", "export"]
DISCOVERY = {"cbr": cbr.ingest, "naufor": naufor.ingest, "afsa": afsa.ingest, "listings": listings.ingest, "seeds": seeds.ingest}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="investor_research")
    ap.add_argument("command", choices=["run", "export", "status"])
    ap.add_argument("--stages", default=",".join(STAGES), help="comma-separated subset of: " + ",".join(STAGES))
    ap.add_argument("--limit", type=int, default=None, help="process at most N items per stage (pilot)")
    ap.add_argument("--refresh", action="store_true", help="ignore checkpoints and re-crawl company websites")
    ap.add_argument("--offline", action="store_true", help="use only the local HTTP cache")
    ap.add_argument("--db", default=str(config.DB_PATH))
    args = ap.parse_args(argv)

    config.LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr), logging.FileHandler(config.LOG_PATH)])
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db = DB(__import__("pathlib").Path(args.db))

    if args.command == "status":
        for t in ["companies", "contacts", "sources", "licenses", "partnership_opportunities", "collection_runs", "run_errors"]:
            print(t, db.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
        return 0
    if args.command == "export":
        export.run(db)
        return 0

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = set(stages) - set(STAGES)
    if unknown:
        ap.error(f"unknown stages: {unknown}")
    fetcher = Fetcher(offline=args.offline)
    db.start_run(",".join(stages))
    log = logging.getLogger("main")
    try:
        for st in stages:
            log.info("=== stage %s", st)
            if st in DISCOVERY:
                n = DISCOVERY[st](db, fetcher, limit=args.limit)
                log.info("%s: %d records", st, n)
            elif st == "dedupe":
                quality.dedupe(db)
            elif st == "enrich":
                enrich.run(db, fetcher, limit=args.limit, refresh=args.refresh)
            elif st == "crosslinks":
                log.info("crosslinks: %d candidates promoted", quality.promote_candidates(db))
                enrich.run(db, fetcher)  # verify the new candidates' websites
            elif st == "quality":
                quality.dedupe(db)
                log.info("contacts: %s", quality.validate_contacts(db))
            elif st == "score":
                scoring.run(db)
            elif st == "export":
                export.run(db)
    except KeyboardInterrupt:
        log.warning("interrupted — progress is checkpointed; re-run the same command to resume")
        db.finish_run("interrupted")
        return 130
    db.finish_run(f"http requests: {fetcher.requests_made}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
