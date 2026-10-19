"""Polite HTTP fetcher: robots.txt, per-host rate limit, retries, on-disk cache.

The cache makes runs resumable and incremental: a page fetched less than
CACHE_TTL_DAYS ago is served from disk instead of re-requesting the site.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import ssl
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import httpx

from . import config

log = logging.getLogger("fetch")


@dataclass
class Page:
    url: str            # requested URL
    final_url: str      # after redirects
    status: int
    content: bytes
    content_type: str
    from_cache: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 300

    def text(self, encoding: str | None = None) -> str:
        if encoding:
            return self.content.decode(encoding, errors="replace")
        ct = self.content_type.lower()
        if "charset=" in ct:
            enc = ct.split("charset=")[-1].split(";")[0].strip()
            try:
                return self.content.decode(enc, errors="replace")
            except LookupError:
                pass
        head = self.content[:3000].lower()
        if b"windows-1251" in head or b"cp1251" in head:
            return self.content.decode("cp1251", errors="replace")
        return self.content.decode("utf-8", errors="replace")


def _ssl_context() -> ssl.SSLContext:
    """Honour SSL_CERT_FILE / REQUESTS_CA_BUNDLE (corporate proxies), else system defaults."""
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return ssl.create_default_context(cafile=cafile if cafile and os.path.exists(cafile) else None)


class RobotsDisallowed(Exception):
    pass


class Fetcher:
    def __init__(self, cache_path=config.CACHE_DB_PATH, ttl_days: int = config.CACHE_TTL_DAYS, offline: bool = False):
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache = sqlite3.connect(cache_path, check_same_thread=False, timeout=60)
        self._cache.execute("CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, final_url TEXT, status INTEGER,"
                            " content BLOB, content_type TEXT, fetched_at TEXT, error TEXT)")
        self._cache_lock = threading.Lock()
        self._host_locks: dict[str, threading.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._global = threading.Lock()
        self.ttl = timedelta(days=ttl_days)
        self.offline = offline
        self.client = httpx.Client(
            headers={"User-Agent": config.USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
            timeout=httpx.Timeout(config.REQUEST_TIMEOUT, connect=12, pool=300),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=16, keepalive_expiry=10),
            follow_redirects=True, verify=_ssl_context())
        self.requests_made = 0

    # --- cache --------------------------------------------------------------
    def _cached(self, url: str) -> Page | None:
        with self._cache_lock:
            row = self._cache.execute("SELECT final_url, status, content, content_type, fetched_at, error FROM pages"
                                      " WHERE url=?", (url,)).fetchone()
        if not row:
            return None
        if row[5] and not self.offline:
            return None  # previous attempt failed: retry
        fetched = datetime.fromisoformat(row[4])
        if not self.offline and datetime.now(timezone.utc) - fetched > self.ttl:
            return None
        return Page(url, row[0], row[1], row[2] or b"", row[3] or "", True, row[5])

    def _store(self, p: Page) -> None:
        with self._cache_lock:
            self._cache.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?)",
                                (p.url, p.final_url, p.status, p.content, p.content_type,
                                 datetime.now(timezone.utc).isoformat(), p.error))
            self._cache.commit()

    # --- politeness ----------------------------------------------------------
    def _host_lock(self, host: str) -> threading.Lock:
        with self._global:
            return self._host_locks.setdefault(host, threading.Lock())

    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        with self._global:
            known = base in self._robots
        if not known:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = self.client.get(base + "/robots.txt", timeout=15)
                if r.status_code == 200 and "text" in r.headers.get("content-type", "text"):
                    rp.parse(r.text.splitlines())
                elif r.status_code in (401, 403):
                    rp.disallow_all = True
                else:
                    rp.allow_all = True
            except httpx.HTTPError:
                rp.allow_all = True  # robots unreachable -> standard practice: treat as allowed
            with self._global:
                self._robots[base] = rp
        rp = self._robots[base]
        return rp.can_fetch(config.USER_AGENT, url) and rp.can_fetch("*", url)

    def get(self, url: str, *, use_cache: bool = True, check_robots: bool = True) -> Page:
        if use_cache:
            cached = self._cached(url)
            if cached is not None:
                return cached
        if self.offline:
            return Page(url, url, 0, b"", "", error="offline: not in cache")
        if check_robots and not self.allowed(url):
            raise RobotsDisallowed(url)
        host = urlsplit(url).netloc
        lock = self._host_lock(host)
        last_err = None
        for attempt in range(config.MAX_RETRIES + 1):
            with lock:
                wait = self._host_last.get(host, 0) + config.PER_HOST_DELAY - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                try:
                    r = self.client.get(url)
                    self.requests_made += 1
                    page = Page(url, str(r.url), r.status_code, r.content, r.headers.get("content-type", ""))
                except httpx.HTTPError as e:
                    last_err = f"{type(e).__name__}: {e}"
                    page = None
                    if isinstance(e, httpx.ConnectError) and "address" in str(e):
                        break  # DNS failure: retrying will not help
                finally:
                    self._host_last[host] = time.monotonic()
            if page is not None and page.status not in (429, 500, 502, 503, 504):
                self._store(page)
                return page
            if page is not None:
                last_err = f"HTTP {page.status}"
            time.sleep(2 * (attempt + 1))
        if url.startswith("https://") and last_err and "SSL" in last_err:
            # Many sites use certificates outside public trust stores; the same public
            # page is often served over plain HTTP.
            alt = self.get("http://" + url[8:], use_cache=use_cache, check_robots=check_robots)
            if alt.ok:
                alt.url = url
                self._store(alt)
                return alt
        page = Page(url, url, page.status if page else 0, b"", "", error=last_err)
        self._store(page)
        return page
