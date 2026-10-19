"""Central configuration: paths, network politeness, keyword dictionaries."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = ROOT / "output"
DB_PATH = OUTPUT_DIR / "bazanov_partners.db"
CACHE_DB_PATH = ROOT / ".cache" / "http_cache.db"  # raw page cache, not committed
SEEDS_DIR = Path(__file__).resolve().parent / "seeds"
LOG_PATH = ROOT / ".cache" / "collector.log"

USER_AGENT = (
    "BazanovTradingResearchBot/1.0 (+public business-contact research; "
    "respects robots.txt; low request rate)"
)
# Minimum delay between two requests to the same host, seconds.
PER_HOST_DELAY = 1.5
REQUEST_TIMEOUT = 25
MAX_RETRIES = 2
# Cached pages younger than this are reused on re-runs (incremental mode).
CACHE_TTL_DAYS = 30
# Pages per company website during enrichment (home + contacts/about).
MAX_PAGES_PER_SITE = 5
WORKERS = 8

COUNTRY_ISO = {
    "Россия": "RU", "Казахстан": "KZ", "Беларусь": "BY", "Узбекистан": "UZ",
    "Армения": "AM", "Азербайджан": "AZ", "Кыргызстан": "KG", "Таджикистан": "TJ",
    "Молдова": "MD", "Туркменистан": "TM",
}

# E-mail domains of mass mail services: such addresses are kept but never
# treated as confirmed corporate e-mail.
FREEMAIL_DOMAINS = {
    "gmail.com", "mail.ru", "yandex.ru", "ya.ru", "bk.ru", "list.ru", "inbox.ru",
    "rambler.ru", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "yandex.kz", "mail.kz", "internet.ru",
}
ROLE_LOCALPARTS = {
    "info", "office", "ir", "invest", "investor", "investors", "contact", "contacts",
    "mail", "post", "sales", "client", "clients", "support", "hello", "partners",
    "partner", "am", "pr", "press", "reception", "secretary", "general", "company",
    "trust", "broker", "dealer", "fund", "funds", "uk", "pif", "npf", "admin",
    "welcome", "service", "help", "corp", "business", "wealth", "pb", "family",
}

# Contact-page discovery on company websites.
CONTACT_LINK_HINTS = (
    "contact", "kontakt", "контакт", "about", "o-kompanii", "о компании", "company",
    "о нас", "team", "команда", "раскрытие", "disclosure",
)

# --- Evidence dictionaries used by classification and scoring -------------
# Each entry: signal name -> list of regex fragments (case-insensitive).
SIGNALS = {
    # Fit with a professional trading business
    "trust_management": [r"доверительн\w* управлени", r"asset management", r"управлени\w* активами",
                         r"wealth management", r"управлени\w* капитал"],
    "hedge_fund": [r"хедж[- ]?фонд", r"hedge fund", r"абсолютн\w* доходност", r"absolute return"],
    "family_office": [r"family office", r"семейн\w* офис", r"multi[- ]family"],
    "prop_trading": [r"проприетарн", r"proprietary trading", r"prop[- ]trading", r"собственн\w* капитал\w* для торгов",
                     r"торгов\w* на собственн\w* средств", r"собственн\w* торгов"],
    "alternative": [r"альтернативн\w* инвестиц", r"alternative investment", r"alternative strateg",
                    r"альтернативн\w* стратеги"],
    "qualified_investors": [r"квалифицированн\w* инвестор", r"qualified investor", r"professional client",
                            r"профессиональн\w* клиент"],
    # Practice of allocating to external managers / financing traders
    "external_managers": [r"внешн\w* управляющ", r"external (asset )?manager", r"фонд\w* фондов", r"fund of funds",
                          r"подбор\w* управляющ", r"manager selection", r"seed (capital|investment)",
                          r"посевн\w* капитал", r"капитал\w* для трейдер", r"финансирован\w* трейдер",
                          r"funded trader", r"инвестиц\w* в трейдинг", r"инвестиц\w* в стратеги",
                          r"capital allocation", r"аллокаци\w* капитал", r"доверяем\w* управляющ"],
    # Commodities / derivatives / quant
    "derivatives": [r"фьючерс", r"futures", r"срочн\w* рын", r"производн\w* финансов\w* инструмент",
                    r"деривати", r"derivative", r"опцион", r"options"],
    "commodities": [r"товарн\w* рын", r"commodit", r"нефт", r"природн\w* газ", r"natural gas",
                    r"crude oil", r"металл", r"metals", r"золот", r"gold"],
    "quant": [r"алгоритмическ", r"algorithmic", r"количественн\w* (стратег|метод|анализ)", r"quantitative",
              r"\bquant\b", r"систематическ\w* (торгов|стратег)", r"systematic", r"роботизирован",
              r"managed futures", r"\bCTA\b", r"маркет[- ]?мейк", r"market[- ]making", r"высокочастотн", r"HFT"],
    # Strategic partnership potential
    "fund_infrastructure": [r"запуск\w* фонд", r"fund launch", r"white[- ]label", r"fund administration",
                            r"администрирован\w* фонд", r"фонд\w* под ключ", r"структурирован\w* фонд"],
    "venture_pe": [r"венчурн", r"venture capital", r"private equity", r"прямых инвестиц", r"direct investment"],
    "fintech": [r"финтех", r"fintech"],
    "investment_holding": [r"инвестиционн\w* холдинг", r"investment holding", r"инвестиционн\w* групп",
                           r"investment group"],
}
