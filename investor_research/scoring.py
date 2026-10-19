"""Bazanov Partnership Score (0-100), confidence and completeness.

Every point is attributed either to a *fact* (regulator/SRO record or text on
the company's own website, stored in `sources`) or to an *inference* from the
organisation's category. Missing data never subtracts points: it simply
earns none, and lowers completeness rather than the score.
Interest is never inferred from company size: size only feeds the
"capacity to provide capital" criterion.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict

from .db import DB

MAX = {"profile_fit": 25, "capital_capacity": 20, "external_managers": 20, "markets_fit": 15,
       "strategic": 10, "contacts": 10}
TIERS = [(80, "Приоритетный партнёр"), (60, "Перспективная организация"), (40, "Дополнительный кандидат"),
         (0, "Низкая релевантность")]
SIGNAL_LABELS = {
    "trust_management": "доверительное управление / управление активами",
    "hedge_fund": "хедж-фонды / стратегии абсолютной доходности",
    "family_office": "family office",
    "prop_trading": "торговля на собственные средства (prop trading)",
    "alternative": "альтернативные инвестиции",
    "qualified_investors": "продукты для квалифицированных инвесторов",
    "external_managers": "работа с внешними управляющими / финансирование трейдеров / фонды фондов",
    "derivatives": "срочный рынок / деривативы / фьючерсы",
    "commodities": "товарные рынки (нефть, газ, металлы)",
    "quant": "алгоритмические / количественные стратегии",
    "fund_infrastructure": "инфраструктура запуска и администрирования фондов",
    "venture_pe": "венчурные / прямые инвестиции",
    "fintech": "финтех",
    "investment_holding": "инвестиционный холдинг / группа",
}


class Score:
    def __init__(self):
        self.parts = defaultdict(float)
        self.fact = defaultdict(float)
        self.reasons: list[str] = []

    def add(self, crit: str, pts: float, reason: str, fact: bool = True):
        room = MAX[crit] - self.parts[crit]
        pts = max(0.0, min(pts, room))
        if pts <= 0:
            return
        self.parts[crit] += pts
        if fact:
            self.fact[crit] += pts
        self.reasons.append(f"{'' if fact else '[предположение] '}{reason} (+{pts:g} {crit})")

    @property
    def total(self) -> float:
        return round(sum(self.parts.values()), 1)


def _capital_rub(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"Уставный капитал:\s*([\d\s,]+)", text)
    if not m:
        return None
    try:
        return float(m.group(1).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def score_company(db: DB, c) -> tuple[Score, dict]:
    cid = c["company_id"]
    s = Score()
    signals: dict[str, list[str]] = defaultdict(list)
    signal_urls: dict[str, str] = {}
    registry = {}
    source_types = set()
    for src in db.conn.execute("SELECT * FROM sources WHERE company_id=?", (cid,)):
        source_types.add(src["source_type"])
        ev = json.loads(src["extracted_evidence"]) if src["extracted_evidence"] else {}
        if src["source_type"] == "company_site":
            for k, v in (ev.get("signals") or {}).items():
                signals[k].extend(v)
                signal_urls.setdefault(k, src["source_url"])
        elif src["source_type"] == "regulator_registry":
            registry = ev | {"url": src["source_url"]}
    licenses = [dict(r) for r in db.conn.execute("SELECT * FROM licenses WHERE company_id=? AND"
                                                 " (status IS NULL OR status NOT LIKE 'Прекращ%')", (cid,))]
    lic_text = " ".join(l["license_type"] for l in licenses).lower()
    cat = (c["category"] or "").lower()
    grp = c["priority_group"] or "D"
    pif = registry.get("pif_categories") or {}
    n_funds = sum(v for k, v in pif.items() if not k.startswith("__") and not k.startswith("ЗПИФ"))
    hist = registry.get("pif_historical_alt") or {}
    zpif_rfi = pif.get("ЗПИФ рыночных финансовых инструментов", 0)

    # 1. Profile fit -----------------------------------------------------------
    base = {"A": 12, "B": 9, "C": 6, "D": 4}.get(grp, 4)
    s.add("profile_fit", base, f"категория: {c['category']}", fact=bool(licenses))
    if pif.get("Хедж"):
        s.add("profile_fit", 7, f"управляет хедж-ПИФ ({pif['Хедж']}) — реестр ЦБ РФ")
    if pif.get("Товарного рынка"):
        s.add("profile_fit", 5, "управляет ПИФ товарного рынка — реестр ЦБ РФ")
    if zpif_rfi:
        s.add("profile_fit", 6, f"управляет закрытыми ПИФ рыночных финансовых инструментов ({zpif_rfi}) — "
                                "современная форма хедж-фонда для квалинвесторов, реестр ЦБ РФ")
    if hist.get("Хедж") or hist.get("Товарного рынка"):
        s.add("profile_fit", 4, "в прошлом управляла хедж-ПИФ/ПИФ товарного рынка — реестр ПИФ ЦБ РФ")
    for sig, pts in (("hedge_fund", 5), ("family_office", 5), ("prop_trading", 4), ("alternative", 3),
                     ("trust_management", 2), ("qualified_investors", 2)):
        if signals.get(sig):
            s.add("profile_fit", pts, f"на сайте: {SIGNAL_LABELS[sig]}")

    # 2. Capacity to provide capital ------------------------------------------
    if "пенсионн" in cat:
        s.add("capital_capacity", 12, "НПФ — лицензия ЦБ РФ, распоряжается пенсионными резервами")
    if n_funds >= 10:
        s.add("capital_capacity", 10, f"{n_funds} действующих ПИФ под управлением — реестр ЦБ РФ")
    elif n_funds >= 3:
        s.add("capital_capacity", 7, f"{n_funds} действующих ПИФ под управлением — реестр ЦБ РФ")
    elif n_funds:
        s.add("capital_capacity", 4, f"{n_funds} действующий(-их) ПИФ — реестр ЦБ РФ")
    if "управлению ценными бумагами" in lic_text or "managing investments" in lic_text:
        s.add("capital_capacity", 5, "лицензия на доверительное управление / Managing Investments")
    if "collective investment" in lic_text:
        s.add("capital_capacity", 6, "лицензия AFSA на управление коллективными инвестициями")
    if "банковских операций" in lic_text or "accepting deposits" in lic_text:
        s.add("capital_capacity", 7, "банковская лицензия")
    if "дилерск" in lic_text or "as principal" in lic_text:
        s.add("capital_capacity", 4, "дилерская лицензия — операции на собственные средства")
    cap = _capital_rub(c["own_capital"])
    if cap:
        if cap >= 1e9:
            s.add("capital_capacity", 6, f"уставный капитал {cap/1e9:.1f} млрд руб. — реестр ЦБ РФ")
        elif cap >= 1e8:
            s.add("capital_capacity", 4, f"уставный капитал {cap/1e6:.0f} млн руб. — реестр ЦБ РФ")
        elif cap >= 2e7:
            s.add("capital_capacity", 2, f"уставный капитал {cap/1e6:.0f} млн руб. — реестр ЦБ РФ")
    if c["aum"]:
        s.add("capital_capacity", 4, f"опубликован объём активов: {c['aum'][:80]}")
    if not licenses and grp in ("A", "C"):
        s.add("capital_capacity", 5, "категория инвестора (без регуляторного подтверждения)", fact=False)

    # 3. Practice of financing traders / external managers ---------------------
    if signals.get("external_managers"):
        s.add("external_managers", 14, f"на сайте: {SIGNAL_LABELS['external_managers']}")
    if pif.get("Фондов"):
        s.add("external_managers", 6, "управляет фондом фондов — реестр ЦБ РФ")
    if hist.get("Фондов"):
        s.add("external_managers", 5, "в прошлом управляла фондом фондов — реестр ПИФ ЦБ РФ")
    if pif.get("Хедж"):
        s.add("external_managers", 4, "хедж-ПИФ — инфраструктура для альтернативных стратегий")
    if signals.get("prop_trading"):
        s.add("external_managers", 4, "проп-трейдинг: капитал под торговые стратегии")
    if signals.get("family_office"):
        s.add("external_managers", 3, "family office обычно распределяет капитал между управляющими", fact=False)

    # 4. Commodities / derivatives / quant -------------------------------------
    for sig, pts in (("derivatives", 5), ("commodities", 5), ("quant", 5)):
        if signals.get(sig):
            s.add("markets_fit", pts, f"на сайте: {SIGNAL_LABELS[sig]}")
    if re.search(r"futures|contracts for differences|options", lic_text):
        s.add("markets_fit", 3, "лицензия AFSA распространяется на фьючерсы/опционы/CFD")
    if pif.get("Товарного рынка"):
        s.add("markets_fit", 5, "ПИФ товарного рынка — реестр ЦБ РФ")
    if hist.get("Товарного рынка"):
        s.add("markets_fit", 4, "опыт управления ПИФ товарного рынка — реестр ПИФ ЦБ РФ")
    if hist.get("Хедж"):
        s.add("markets_fit", 3, "опыт управления хедж-ПИФ — реестр ПИФ ЦБ РФ")
    if zpif_rfi:
        s.add("markets_fit", 2, "ЗПИФ РФИ может использовать производные инструменты — реестр ЦБ РФ")
    if pif.get("Хедж"):
        s.add("markets_fit", 3, "хедж-ПИФ может использовать деривативы — реестр ЦБ РФ")

    # 5. Strategic partnership -----------------------------------------------
    if n_funds or "collective investment" in lic_text:
        s.add("strategic", 4, "может выступить УК для совместного фонда под стратегию")
    for sig, pts in (("fund_infrastructure", 4), ("investment_holding", 3), ("venture_pe", 2), ("fintech", 2),
                     ("family_office", 3)):
        if signals.get(sig):
            s.add("strategic", pts, f"на сайте: {SIGNAL_LABELS[sig]}")
    if "брокер" in lic_text or "as agent" in lic_text:
        s.add("strategic", 2, "брокерская инфраструктура (доступ к рынкам, клиентская база)")

    # 6. Direct corporate contacts ----------------------------------------------
    contacts = db.conn.execute("SELECT * FROM contacts WHERE company_id=? AND verification_status='confirmed'",
                               (cid,)).fetchall()
    if any(r["contact_type"] == "email" for r in contacts):
        s.add("contacts", 5, "подтверждённый корпоративный email")
    if any(r["contact_type"] == "phone" for r in contacts):
        s.add("contacts", 3, "подтверждённый телефон")
    if c["website_status"] == "ok" or (c["website_status"] or "").startswith("redirect"):
        s.add("contacts", 2, "сайт доступен")

    meta = {"signals": signals, "signal_urls": signal_urls, "registry": registry, "licenses": licenses,
            "source_types": source_types, "pif": pif, "contacts": contacts}
    return s, meta


COMPLETENESS_FIELDS = ["brand_name", "city", "website", "category", "description", "investment_focus",
                       "regulatory_status", "head_name"]


def opportunities(c, s: Score, meta) -> list[tuple]:
    sig, pif, lic = meta["signals"], meta["pif"], " ".join(l["license_type"] for l in meta["licenses"]).lower()
    out = []
    ext_fact = bool(sig.get("external_managers"))
    if ext_fact or sig.get("family_office"):
        out.append(("Выделение капитала Bazanov Trading как внешнему управляющему (мандат/субсчёт)",
                    "Организация публично упоминает работу с внешними управляющими/распределение капитала"
                    if ext_fact else "Family office, как правило, распределяет капитал между управляющими",
                    "; ".join((sig.get("external_managers") or sig.get("family_office"))[:2]),
                    "Требуется подтверждённый трек-рекорд (обычно 2-3 года), аудит, соответствие риск-лимитам",
                    "high" if ext_fact else "medium", "fact" if ext_fact else "analytical_assumption"))
    if pif or "collective investment" in lic:
        hedge = bool(pif.get("Хедж") or pif.get("Товарного рынка"))
        out.append(("Совместный фонд (ЗПИФ / хедж-ПИФ / фонд МФЦА) под стратегию Bazanov Trading",
                    "УК уже управляет " + ("хедж-ПИФ/ПИФ товарного рынка" if hedge else "паевыми/коллективными фондами")
                    + " и может выступить управляющей компанией продукта",
                    json.dumps(pif, ensure_ascii=False) if pif else lic[:300],
                    "Регуляторные требования к инвестиционной декларации; возможно, только для квалифицированных инвесторов",
                    "high" if hedge else "medium", "fact" if hedge else "analytical_assumption"))
    if "управлению ценными бумагами" in lic or "managing investments" in lic or sig.get("trust_management"):
        out.append(("Партнёрство в доверительном управлении (стратегия Bazanov в продуктовой линейке)",
                    "Компания оказывает услуги доверительного управления и может включить стратегию в свою линейку",
                    "; ".join(sig.get("trust_management", [])[:1]) or "лицензия на ДУ",
                    "Стратегия должна соответствовать инвестиционному профилю клиентов и требованиям к ДУ",
                    "medium", "analytical_assumption"))
    if sig.get("prop_trading") or "дилерск" in lic or "as principal" in lic:
        out.append(("Торговля капиталом организации (prop-мандат / совместный торговый деск)",
                    "Организация ведёт операции на собственные средства",
                    "; ".join(sig.get("prop_trading", [])[:1]) or "дилерская лицензия",
                    "Внутренние риск-политики; вероятно требование работать в штате/на инфраструктуре организации",
                    "medium", "fact" if sig.get("prop_trading") else "analytical_assumption"))
    if sig.get("venture_pe") or sig.get("investment_holding") or sig.get("fintech"):
        out.append(("Стратегическая инвестиция / долевое участие в Bazanov Trading",
                    "Организация осуществляет прямые/венчурные инвестиции или является инвестиционным холдингом",
                    "; ".join((sig.get("venture_pe") or sig.get("investment_holding") or sig.get("fintech"))[:1]),
                    "Фокус на масштабируемом бизнесе; нужна бизнес-модель с управляемым капиталом и выручкой",
                    "medium", "analytical_assumption"))
    if "брокер" in lic or "as agent" in lic:
        out.append(("Инфраструктурное партнёрство: брокерское обслуживание, доступ к срочному/товарному рынку, "
                    "привлечение клиентов",
                    "Лицензированный брокер с клиентской базой и доступом к рынкам",
                    "лицензия брокера", "Конфликт интересов с собственными продуктами брокера",
                    "low", "analytical_assumption"))
    if "пенсионн" in (c["category"] or "").lower() or "страхов" in (c["category"] or "").lower():
        out.append(("Институциональный мандат (через УК) после накопления трек-рекорда",
                    "Институциональный инвестор с долгосрочными резервами",
                    "лицензия ЦБ РФ", "Жёсткие регуляторные ограничения на структуру активов; низкая доля альтернатив",
                    "low", "analytical_assumption"))
    return out


def run(db: DB) -> int:
    rows = db.conn.execute("SELECT * FROM companies WHERE excluded_reason IS NULL").fetchall()
    for c in rows:
        s, meta = score_company(db, c)
        total = s.total
        fact_pts = sum(s.fact.values())
        n_types = len(meta["source_types"])
        confidence = round(70 * (fact_pts / total if total else 0) + 30 * min(1.0, n_types / 3), 1)
        filled = sum(1 for f in COMPLETENESS_FIELDS if c[f])
        has_email = any(r["contact_type"] == "email" for r in meta["contacts"])
        has_phone = any(r["contact_type"] == "phone" for r in meta["contacts"])
        completeness = round(100 * (filled + has_email + has_phone + bool(c["aum"] or c["own_capital"]))
                             / (len(COMPLETENESS_FIELDS) + 3), 1)
        tier = next(t for th, t in TIERS if total >= th)
        top_reasons = [r for r in s.reasons if "contacts)" not in r][:6]
        breakdown = {k: round(v, 1) for k, v in s.parts.items()} | {"facts_share": round(fact_pts / total, 2) if total else 0,
                                                                   "reasons": s.reasons}
        db.update_company(c["company_id"], partnership_score=total, confidence_score=confidence,
                          completeness_score=completeness, tier=tier,
                          relevance_reason="; ".join(top_reasons)[:1500],
                          score_breakdown=json.dumps(breakdown, ensure_ascii=False))
        with db.lock:
            db.conn.execute("DELETE FROM partnership_opportunities WHERE company_id=?", (c["company_id"],))
            for (ptype, rationale, evidence, constraints, priority, basis) in opportunities(c, s, meta):
                db.conn.execute("INSERT INTO partnership_opportunities(company_id, partnership_type, rationale,"
                                " supporting_evidence, constraints, priority, basis) VALUES (?,?,?,?,?,?,?)",
                                (c["company_id"], ptype, rationale, evidence[:1500], constraints, priority, basis))
            db.conn.commit()
    return len(rows)
