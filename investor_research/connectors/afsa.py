"""AFSA (Astana International Financial Centre) public register of authorised firms.

Provides legal names, licence numbers, regulated activities and approved
individuals (Senior Executive Officer etc.). It does not publish websites or
contacts; those come from curated seeds and website enrichment.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..db import DB
from ..fetch import Fetcher
from ..normalize import clean_text, name_key

log = logging.getLogger("afsa")
BASE = "https://publicreg.myafsa.com"
LIST = BASE + "/authorised/?page={page}"

# Activities that make a firm a potential capital / trading partner.
RELEVANT = {
    "Managing Investments": ("Компания по управлению активами (AIFC)", "A"),
    "Managing a Collective Investment Scheme": ("Управляющий инвестиционными фондами (AIFC)", "A"),
    "Managing a Collective Investment Scheme in respect of Exempt Funds": ("Управляющий фондами для профинвесторов (AIFC)", "A"),
    "Dealing in Investments as Principal": ("Дилер / торговля на собственные средства (AIFC)", "B"),
    "Dealing in Investments as Principal (on a non-matched basis)": ("Дилер / торговля на собственные средства (AIFC)", "B"),
    "Dealing in Investments as Agent": ("Брокер (AIFC)", "B"),
    "Advising on Investments": ("Инвестиционный консультант (AIFC)", "D"),
    "Arranging Deals in Investments": ("Инвестиционный посредник (AIFC)", "D"),
    "Providing Fund Administration": ("Администратор фондов (AIFC)", "D"),
    "Acting as the Trustee of a Fund": ("Трасти фондов (AIFC)", "D"),
    "Operating a Representative Office": ("Представительство финансовой организации (AIFC)", "C"),
    "Accepting Deposits": ("Банк (AIFC)", "C"),
}
RANK = ["Managing a Collective Investment Scheme", "Managing a Collective Investment Scheme in respect of Exempt Funds",
        "Managing Investments", "Dealing in Investments as Principal (on a non-matched basis)",
        "Dealing in Investments as Principal", "Dealing in Investments as Agent", "Accepting Deposits",
        "Providing Fund Administration", "Acting as the Trustee of a Fund", "Arranging Deals in Investments",
        "Advising on Investments", "Operating a Representative Office"]


def _rows(html: str):
    soup = BeautifulSoup(html, "lxml")
    for tr in soup.select("table.table tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        a = tds[5].find("a")
        yield {"name": clean_text(tds[0].get_text()), "licence": clean_text(tds[1].get_text()),
               "status": clean_text(tds[2].get_text()), "date": clean_text(tds[3].get_text()),
               "activities_raw": clean_text(tds[4].get_text(" ")),
               "activities": [clean_text(x).lstrip("-").strip() for x in
                              re.split(r"[\n;]", re.sub(r"\([^)]*\)", "", tds[4].get_text("\n"))) if clean_text(x)],
               "details": BASE + a["href"] if a else None}


def _details(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "lxml")
    text = [clean_text(t) for t in soup.get_text("\n").split("\n")]
    text = [t for t in text if t]
    people = []
    if "Approved individuals" in text:
        i = text.index("Approved individuals")
        rest = text[i + 1:]
        for j in range(len(rest) - 2):
            if rest[j + 1] in ("Senior Executive Officer", "Director", "Finance Officer", "Compliance Officer",
                               "Money Laundering Reporting Officer", "Chief Executive Officer") and rest[j + 2] == "Active":
                people.append((rest[j].title(), rest[j + 1]))
    return people


def ingest(db: DB, fetcher: Fetcher, limit: int | None = None) -> int:
    rows = []
    page_no = 1
    while page_no < 60:
        url = LIST.format(page=page_no)
        p = fetcher.get(url)
        if not p.ok:
            if page_no == 1:
                db.error("afsa", url, p.error or p.status)
                db.coverage("afsa_authorised", "Казахстан", url, "unreachable", 0)
                return 0
            break
        batch = list(_rows(p.text()))
        if not batch:
            break
        rows.extend(batch)
        if f"?page={page_no + 1}" not in p.text():
            break
        page_no += 1
    log.info("AFSA: %d authorised-firm rows", len(rows))
    n = 0
    for r in rows:
        if (r["status"] or "").lower() != "active":
            continue
        acts = r["activities"]
        relevant = [a for a in RANK if a in acts]
        if not relevant:
            continue
        if limit and n >= limit:
            break
        cat, grp = RELEVANT[relevant[0]]
        people = []
        if r["details"]:
            d = fetcher.get(r["details"])
            if d.ok:
                people = _details(d.text())
        seo = next((p for p in people if p[1] in ("Senior Executive Officer", "Chief Executive Officer")), None)
        cid = db.upsert_company(
            legal_name=r["name"], name_key=name_key(r["name"]), country="Казахстан",
            identifiers={"afsa_licence": r["licence"]}, city="Астана", category=cat, priority_group=grp,
            regulatory_status=f"AFSA (МФЦА) лицензия {r['licence']} от {r['date']}: {r['activities_raw']}"[:900],
            head_name=seo[0] if seo else None, head_position=seo[1] if seo else None)
        db.add_license(cid, "AFSA (AIFC)", (r["activities_raw"] or "")[:500], r["licence"], r["status"], r["date"],
                       r["details"] or LIST.format(page=1))
        db.add_source(cid, r["details"] or LIST.format(page=1), "regulator_registry",
                      {"registry": "AFSA Public Register", "activities": acts,
                       "senior_executive": f"{seo[0]} ({seo[1]})" if seo else None})
        n += 1
    db.coverage("afsa_authorised", "Казахстан", BASE + "/authorised/", "processed", n,
                f"{len(rows)} firms listed, {n} with relevant activities")
    return n
