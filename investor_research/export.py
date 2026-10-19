"""CSV exports, research report and the top-100 partner dossier."""
from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone

from . import config
from .db import DB

COLUMNS = ["company_id", "legal_name", "brand_name", "country", "city", "category", "priority_group", "website",
           "website_status", "primary_email", "all_confirmed_emails", "primary_phone", "all_confirmed_phones",
           "contact_page", "contacts_source_urls", "description", "aum", "own_capital", "investment_focus",
           "instruments", "allocates_external", "regulatory_status", "head_name", "head_position",
           "partnership_score", "tier", "confidence_score", "completeness_score", "contact_verification",
           "relevance_reason", "partnership_formats", "evidence_sources", "last_verified_at"]


def _rows(db: DB) -> list[dict]:
    out = []
    comps = db.conn.execute("SELECT * FROM companies WHERE excluded_reason IS NULL"
                            " ORDER BY partnership_score DESC, confidence_score DESC").fetchall()
    for c in comps:
        cid = c["company_id"]
        contacts = db.conn.execute("SELECT * FROM contacts WHERE company_id=? AND verification_status='confirmed'"
                                   " ORDER BY is_role_address DESC, CASE source_kind WHEN 'regulator' THEN 0 WHEN 'sro'"
                                   " THEN 1 WHEN 'company_site' THEN 2 ELSE 3 END", (cid,)).fetchall()
        emails = [r["contact_value"] for r in contacts if r["contact_type"] == "email"]
        phones = [r["contact_value"] for r in contacts if r["contact_type"] == "phone"]
        page = next((r["contact_value"] for r in contacts if r["contact_type"] == "contact_page"), None)
        csrc = sorted({r["source_url"] for r in contacts if r["contact_type"] in ("email", "phone")})
        ops = db.conn.execute("SELECT partnership_type, priority, basis FROM partnership_opportunities WHERE company_id=?"
                              " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END", (cid,)).fetchall()
        srcs = db.conn.execute("SELECT DISTINCT source_url FROM sources WHERE company_id=? AND source_type!='company_site'"
                               " UNION SELECT source_url FROM (SELECT source_url FROM sources WHERE company_id=? AND"
                               " source_type='company_site' LIMIT 3)", (cid, cid)).fetchall()
        bd = json.loads(c["score_breakdown"]) if c["score_breakdown"] else {}
        out.append({
            **{k: c[k] for k in ("company_id", "legal_name", "brand_name", "country", "city", "category", "priority_group",
                                 "website", "website_status", "description", "aum", "own_capital", "investment_focus",
                                 "instruments", "allocates_external", "regulatory_status", "head_name", "head_position",
                                 "partnership_score", "tier", "confidence_score", "completeness_score",
                                 "relevance_reason", "last_verified_at")},
            "primary_email": emails[0] if emails else None,
            "all_confirmed_emails": "; ".join(emails[:6]) or None,
            "primary_phone": phones[0] if phones else None,
            "all_confirmed_phones": "; ".join(phones[:6]) or None,
            "contact_page": page,
            "contacts_source_urls": " ".join(csrc[:4]) or None,
            "contact_verification": "confirmed" if (emails or phones) else "insufficient",
            "partnership_formats": " | ".join(f"{o['partnership_type']} [{o['priority']}; "
                                              f"{'факт' if o['basis'] == 'fact' else 'предположение'}]" for o in ops) or None,
            "evidence_sources": " ".join(r[0] for r in srcs) or None,
            "_breakdown": bd,
        })
    return out


def _write(path, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("NULL" if r.get(k) in (None, "") else r[k]) for k in COLUMNS})


def is_capital_source(r) -> bool:
    bd = r["_breakdown"]
    return r["priority_group"] in ("A", "C") and (bd.get("capital_capacity", 0) >= 7 or r["allocates_external"])


def is_trading_partner(r) -> bool:
    bd = r["_breakdown"]
    cat = (r["category"] or "").lower()
    return (r["priority_group"] == "B" or bd.get("markets_fit", 0) >= 5 or
            any(w in cat for w in ("хедж", "проп", "трейдинг", "дилер")))


def is_allocator(r) -> bool:
    """Organisations with evidence of investing in external managers / trading strategies."""
    reasons = " ".join(r["_breakdown"].get("reasons", []))
    return bool(r["allocates_external"]) or "фондом фондов" in reasons or "хедж-ПИФ" in reasons


def run(db: DB) -> None:
    config.OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = _rows(db)
    _write(config.OUTPUT_DIR / "bazanov_partners.csv", rows)
    pri = [r for r in rows if is_capital_source(r) and (r["partnership_score"] or 0) >= 40]
    _write(config.OUTPUT_DIR / "priority_investors.csv", pri)
    _write(config.OUTPUT_DIR / "trading_partners.csv", [r for r in rows if is_trading_partner(r)])
    _write(config.OUTPUT_DIR / "institutional_investors.csv", [r for r in rows if r["priority_group"] == "C"])
    _write(config.OUTPUT_DIR / "external_manager_allocators.csv", [r for r in rows if is_allocator(r)])
    top = top100(rows)
    write_top100(db, top)
    write_report(db, rows, pri, top)


def top100(rows: list[dict]) -> list[dict]:
    # Contactable organisations first; a capital source without any confirmed contact
    # is still listed but after those that can actually be reached.
    ranked = sorted(rows, key=lambda r: (-(r["partnership_score"] or 0), r["contact_verification"] != "confirmed",
                                         -(r["confidence_score"] or 0)))
    return ranked[:100]


def write_top100(db: DB, top: list[dict]) -> None:
    lines = ["# Топ-100 потенциальных партнёров Bazanov Trading", "",
             f"Сформировано автоматически {datetime.now(timezone.utc):%Y-%m-%d} из базы `bazanov_partners.db`. "
             "Пометка «[предположение]» означает аналитический вывод из категории организации, а не "
             "опубликованный факт. Контакты — только подтверждённые публичные корпоративные.", ""]
    for i, r in enumerate(top, 1):
        cid = r["company_id"]
        ops = db.conn.execute("SELECT * FROM partnership_opportunities WHERE company_id=? ORDER BY CASE priority"
                              " WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END", (cid,)).fetchall()
        bd = r["_breakdown"]
        facts = [x for x in bd.get("reasons", []) if not x.startswith("[предположение]") and "contacts)" not in x]
        assumptions = [x for x in bd.get("reasons", []) if x.startswith("[предположение]")]
        lines += [f"## {i}. {r['brand_name'] or r['legal_name']} — {r['partnership_score']:g}/100",
                  "",
                  f"- **Полное название:** {r['legal_name']}",
                  f"- **Страна / город:** {r['country']} / {r['city'] or 'NULL'}",
                  f"- **Категория:** {r['category']} (группа {r['priority_group']}); {r['tier']}; "
                  f"достоверность {r['confidence_score']:g}, полнота {r['completeness_score']:g}",
                  f"- **Сайт:** {r['website'] or 'NULL'}"
                  f"{' (' + r['website_status'] + ')' if r['website_status'] and r['website_status'] != 'ok' else ''}",
                  f"- **Подтверждённые контакты:** email {r['all_confirmed_emails'] or 'NULL'}; "
                  f"тел. {r['all_confirmed_phones'] or 'NULL'}; страница контактов {r['contact_page'] or 'NULL'}",
                  f"- **Инвестиционный профиль:** {r['investment_focus'] or r['description'] or 'NULL'}"
                  + (f"; инструменты: {r['instruments']}" if r["instruments"] else "")
                  + (f"; капитал: {r['own_capital']}" if r["own_capital"] else "")
                  + (f"; AUM: {r['aum']}" if r["aum"] else ""),
                  "- **Доказательства подходящей деятельности:** " + ("; ".join(facts[:6]) or "NULL"),
                  ]
        if assumptions:
            lines.append("- **Аналитические предположения:** " + "; ".join(a.replace("[предположение] ", "") for a in assumptions))
        if ops:
            lines.append("- **Возможный формат сотрудничества:** " + "; ".join(
                f"{o['partnership_type']} ({o['priority']})" for o in ops[:3]))
            lines.append("- **Аргументы в пользу обращения:** " + "; ".join(o["rationale"] for o in ops[:2]))
            lines.append("- **Основные препятствия:** " + "; ".join(sorted({o["constraints"] for o in ops[:3]})))
        lines.append("- **Рекомендуемый адресат:** " + recommended_addressee(r))
        lines.append("- **Источники подтверждения:** " + (r["evidence_sources"] or "NULL"))
        lines.append("")
    (config.OUTPUT_DIR / "top_100_partners.md").write_text("\n".join(lines), encoding="utf-8")


def recommended_addressee(r) -> str:
    cat = (r["category"] or "").lower()
    head = f" (руководитель по данным регулятора: {r['head_name']}, {r['head_position']})" if r["head_name"] else ""
    if "пенсион" in cat or "страх" in cat:
        return "Директор по инвестициям / инвестиционный комитет" + head
    if "банк" in cat:
        return "Руководитель инвестиционного бизнеса / Private Banking / казначейства" + head
    if "управля" in cat or "актив" in cat or "фонд" in cat:
        return "Директор по инвестициям (CIO) / руководитель направления альтернативных стратегий и новых продуктов" + head
    if "дилер" in cat or "брокер" in cat or "инвестиционная компания" in cat:
        return "Руководитель департамента торговых операций / собственных операций (prop desk)" + head
    if "холдинг" in cat or "суверен" in cat or "государствен" in cat:
        return "Директор по инвестициям / дирекция по прямым инвестициям" + head
    return "Генеральный директор / отдел развития бизнеса" + head


def _pct(a, b):
    return f"{(100 * a / b):.1f}%" if b else "—"


def write_report(db: DB, rows, pri, top) -> None:
    q = db.conn.execute
    n = len(rows)
    by_country = Counter(r["country"] for r in rows)
    by_group = Counter(r["priority_group"] for r in rows)
    by_cat = Counter(r["category"] for r in rows)
    tiers = Counter(r["tier"] for r in rows)
    with_site = sum(1 for r in rows if r["website"])
    site_ok = sum(1 for r in rows if (r["website_status"] or "") == "ok" or (r["website_status"] or "").startswith("redirect"))
    with_email = sum(1 for r in rows if r["primary_email"])
    with_phone = sum(1 for r in rows if r["primary_phone"])
    confirmed_any = sum(1 for r in rows if r["contact_verification"] == "confirmed")
    n_contacts = q("SELECT COUNT(*) FROM contacts c JOIN companies m USING(company_id) WHERE m.excluded_reason IS NULL"
                   " AND verification_status='confirmed' AND contact_type IN ('email','phone')").fetchone()[0]
    n_unverified = q("SELECT COUNT(*) FROM contacts WHERE verification_status!='confirmed'").fetchone()[0]
    traced = q("SELECT COUNT(DISTINCT company_id) FROM sources").fetchone()[0]
    proven = sum(1 for r in rows if any(not x.startswith("[предположение]") for x in r["_breakdown"].get("reasons", [])
                                        if "profile_fit" in x or "capital_capacity" in x))
    allocators = [r for r in rows if is_allocator(r)]
    insufficient = [r for r in rows if r["contact_verification"] != "confirmed" and not r["website"]]
    coverage = q("SELECT * FROM source_coverage ORDER BY status, channel").fetchall()
    runs = q("SELECT * FROM collection_runs ORDER BY run_id").fetchall()
    errors = q("SELECT stage, COUNT(*) FROM run_errors GROUP BY stage").fetchall()
    unreachable = sum(1 for r in rows if r["website_status"] and not (r["website_status"] == "ok" or r["website_status"].startswith("redirect")))
    fill = {f: sum(1 for r in rows if r[f]) for f in ("website", "city", "description", "aum", "own_capital",
                                                       "investment_focus", "instruments", "head_name", "regulatory_status")}

    L = ["# Bazanov Trading — отчёт о поиске инвесторов и стратегических партнёров", "",
         f"Дата формирования: {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC. Все цифры ниже вычислены из "
         "`bazanov_partners.db` скриптом `python -m investor_research export`.", "",
         "## 1. Ключевые показатели", "",
         "| Показатель | Значение |", "|---|---|",
         f"| Уникальных организаций в базе | {n} |",
         f"| С сайтом (из реестра или проверенного списка) | {with_site} ({_pct(with_site, n)}) |",
         f"| Сайт доступен при проверке | {site_ok} |",
         f"| С подтверждённым корпоративным email | {with_email} ({_pct(with_email, n)}) |",
         f"| С подтверждённым публичным телефоном | {with_phone} ({_pct(with_phone, n)}) |",
         f"| С хотя бы одним подтверждённым контактом | {confirmed_any} ({_pct(confirmed_any, n)}) |",
         f"| Подтверждённых контактов (email + телефон) | {n_contacts} |",
         f"| Неподтверждённых/отклонённых контактов (не используются в экспорте) | {n_unverified} |",
         f"| С регуляторно/документально подтверждённой инвестиционной деятельностью | {proven} |",
         f"| Потенциальных источников капитала (priority_investors.csv) | {len(pri)} |",
         f"| Высокоприоритетных кандидатов (score ≥ 60) | {sum(1 for r in rows if (r['partnership_score'] or 0) >= 60)} |",
         f"| Уже инвестируют во внешних управляющих / альтернативные стратегии | {len(allocators)} |",
         f"| Записей с прослеживаемыми первоисточниками | {traced} ({_pct(traced, n)}) |", "",
         "## 2. Распределение по странам", "", "| Страна | Организаций |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in by_country.most_common()]
    L += ["", "## 3. Распределение по группам и категориям", "",
          "| Группа | Организаций |", "|---|---|"]
    names = {"A": "A — инвесторы (фонды, УК, управляющие)", "B": "B — профессиональные торговые партнёры",
             "C": "C — институциональные инвесторы", "D": "D — стратегические и смежные"}
    L += [f"| {names.get(k, k)} | {v} |" for k, v in sorted(by_group.items(), key=lambda kv: kv[0] or "")]
    L += ["", "| Категория | Организаций |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in by_cat.most_common()]
    L += ["", "## 4. Распределение по Bazanov Partnership Score", "", "| Класс | Организаций |", "|---|---|"]
    for t in ("Приоритетный партнёр", "Перспективная организация", "Дополнительный кандидат", "Низкая релевантность"):
        L.append(f"| {t} | {tiers.get(t, 0)} |")
    L += ["", "Модель оценки (максимум баллов): соответствие профилю 25, способность предоставлять капитал 20, "
          "практика финансирования внешних управляющих 20, товарные рынки/деривативы/квант 15, стратегическое "
          "партнёрство 10, прямые контакты 10. Баллы начисляются только за найденные признаки; отсутствие данных "
          "не снижает оценку. Размер компании влияет только на критерий «способность предоставлять капитал». "
          "`confidence_score` = 70 × доля баллов, подтверждённых фактами + 30 × разнообразие типов источников; "
          "`completeness_score` = доля заполненных ключевых полей.", "",
          "Высоких оценок (80+) в автоматической модели почти нет: в открытых источниках организации редко прямо "
          "пишут о финансировании внешних трейдеров. Поэтому для приоритизации главными сигналами стоит считать "
          "группу «уже инвестируют во внешних управляющих» и относительный рейтинг.", "",
          "## 5. Наиболее перспективные организации (топ-25)", "",
          "| # | Организация | Страна | Категория | Score | Достоверность | Email | Телефон |", "|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(top[:25], 1):
        L.append(f"| {i} | {r['brand_name'] or r['legal_name']} | {r['country']} | {r['category']} | "
                 f"{r['partnership_score']:g} | {r['confidence_score']:g} | {r['primary_email'] or 'NULL'} | "
                 f"{r['primary_phone'] or 'NULL'} |")
    L += ["", "Полное досье на 100 организаций: `top_100_partners.md`.", "",
          "## 6. Организации, уже инвестирующие во внешних управляющих / альтернативные стратегии", "",
          "Критерии: на собственном сайте найдены упоминания работы с внешними управляющими, фондов фондов, "
          "финансирования трейдеров, посевного капитала; либо по реестру ЦБ РФ организация управляет хедж-ПИФ "
          "или фондом фондов. Полный список: `external_manager_allocators.csv`.", "",
          "| Организация | Страна | Score | Основание |", "|---|---|---|---|"]
    for r in allocators[:60]:
        reasons = [x for x in r["_breakdown"].get("reasons", []) if "external_managers" in x]
        L.append(f"| {r['brand_name'] or r['legal_name']} | {r['country']} | {r['partnership_score']:g} | "
                 f"{'; '.join(reasons)[:220]} |")
    L += ["", "## 7. Качество данных", "",
          "Поля «заполнено» по базе:", "", "| Поле | Заполнено |", "|---|---|"]
    L += [f"| {k} | {v} ({_pct(v, n)}) |" for k, v in fill.items()]
    L += ["", "Правила проверки контактов:",
          "- **confirmed**: контакт опубликован регулятором (Банк России), СРО (НАУФОР), биржей или ассоциацией; "
          "либо найден на официальном сайте, связь которого с организацией подтверждена реестром или наличием "
          "названия организации на сайте, а домен email совпадает с доменом сайта.",
          "- **unverified**: адреса на бесплатных почтовых доменах, адреса на чужих доменах, контакты с сайтов, "
          "принадлежность которых не подтверждена. В CSV-экспорт не попадают.",
          "- Мобильные номера и адреса на бесплатных почтовых доменах из списков членов бирж не сохраняются: "
          "часто это личные контакты.",
          "- Адреса сотрудников по шаблонам не угадывались. Телефоны нормализованы в E.164 (библиотека phonenumbers).",
          f"- Сайтов, недоступных или закрытых robots.txt при проверке: {unreachable}.",
          f"- Организаций без сайта и без подтверждённых контактов (недостаточно информации): {len(insufficient)}.", "",
          "Ошибки сбора по этапам (подробно в таблице `run_errors`):", "", "| Этап | Ошибок |", "|---|---|"]
    L += [f"| {e[0]} | {e[1]} |" for e in errors]
    L += ["", "## 8. Покрытие источников", "", "| Канал | Страна | Статус | Записей | Примечание |", "|---|---|---|---|---|"]
    L += [f"| {c['channel']} | {c['country'] or ''} | {c['status']} | {c['records']} | {c['note'] or ''} |" for c in coverage]
    L += ["", "Запуски сбора:", "", "| run_id | Начало | Окончание | Этапы | Новых | Обновлено | Ошибок |", "|---|---|---|---|---|---|---|"]
    L += [f"| {r['run_id']} | {r['started_at']} | {r['completed_at'] or '—'} | {r['stages']} | {r['new_companies']} | "
          f"{r['updated_companies']} | {r['errors']} |" for r in runs]
    L += ["", "## 9. Ограничения исследования", "",
          "1. **Неравномерный охват по странам.** Россия покрыта официальными реестрами (Банк России, НАУФОР) почти "
          "полностью в части лицензированных участников. По Казахстану доступен только реестр МФЦА (AFSA). Реестр "
          "АРРФР и список членов KASE получить не удалось: сайт АРРФР недоступен из среды сбора, KASE блокирует "
          "автоматический доступ через WAF, обход не выполнялся. Остальные страны покрыты списками членов "
          "ассоциаций/бирж (Армения, Кыргызстан) и отобранным вручную перечнем ключевых организаций с проверкой их "
          "официальных сайтов.",
          "2. **Нелицензируемые участники.** Проп-трейдинговые фирмы, family offices и частные инвестиционные "
          "компании часто не входят в реестры и почти не публикуют контакты. Их охват ограничен. В открытом "
          "доступе нет надёжного каталога family offices СНГ.",
          "3. **Сигналы интереса берутся из текста сайтов** (словари в `config.SIGNALS`). Возможны ложные "
          "срабатывания: например, слово «металл» в описании банковских услуг. Поэтому для топ-100 приведены "
          "фрагменты текста, и их нужно проверить вручную перед обращением.",
          "4. **AUM.** В реестрах ЦБ РФ объёмы активов под управлением не публикуются. Поле `aum` заполнено, "
          "только если сумма явно указана на сайте компании. Уставный капитал — это не AUM.",
          "5. **Сайты** на JavaScript-фреймворках без серверного рендеринга и сайты, закрытые robots.txt, "
          "не обходились, поэтому сигналы по ним могут отсутствовать.",
          "6. **Поисковые системы не опрашивались автоматически**: их условия использования запрещают "
          "парсинг выдачи. Новые организации обнаруживаются через реестры, списки членов и перекрёстные ссылки, "
          "а отобранные вручную организации внесены в `investor_research/seeds/*.csv` со ссылкой на источник.", "",
          "## 10. Источники для расширения охвата", "",
          "- Реестр лицензиатов АРРФР (Казахстан) — при доступе из казахстанского IP или по официальному запросу.",
          "- Список членов KASE — выгрузка по запросу к бирже или ручная выгрузка.",
          "- Список участников торгов Московской биржи, срочного и товарного рынков (moex.com) — недоступен из "
          "среды сбора; рекомендуется запустить конвейер из российской сети.",
          "- Реестр членов НЛУ (Национальная лига управляющих), рейтинги RAEX/НРА по управляющим компаниям.",
          "- Реестры ЦБ Узбекистана / НАПП (cbu.uz, uzse.uz), ЦБ Армении (cba.am), ЦБ Азербайджана (cbar.az), "
          "НБ Беларуси, Moldova NCFM.",
          "- Списки спонсоров и участников конференций: Cbonds, Smart-Lab Conference, Финансовый конгресс Банка "
          "России, Astana Finance Days, Tashkent Investment Forum.",
          "- Каталоги family offices (Campden, FINTRAIL и др.) — платные, в работе не использовались.", ""]
    (config.OUTPUT_DIR / "research_report.md").write_text("\n".join(L), encoding="utf-8")
