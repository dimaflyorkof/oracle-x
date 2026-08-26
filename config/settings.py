from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

APP_NAME = "ORACLE X CORE 1.0"
APP_VERSION = "1.0.0"

DATA_DIR = BASE_DIR / "data"
DB_DIR = BASE_DIR / "database"
LOG_DIR = BASE_DIR / "logs"

DB_PATH = DB_DIR / "oracle_x.db"

# Historical Intelligence
HISTORY_DAYS_SHORT = 30
HISTORY_DAYS_CORE = 180      # основна пам'ять — 6 місяців
HISTORY_DAYS_LONG = 1095     # до 3 років

DEFAULT_SYMBOLS = [
    "BTC",
    "ETH",
    "SOL",
    "BNB",
    "XRP",
    "ADA",
    "DOGE",
    "AVAX",
    "LINK",
    "TON",
]

TIMEFRAMES = [
    "5m",
    "15m",
    "1h",
    "4h",
    "1d",
]

SOURCE_GROUPS = [
    "spot",
    "derivatives",
    "options",
    "orderflow",
    "liquidations",
    "onchain",
    "etf",
    "macro",
    "sentiment",
]

ORACLE_AGENTS = [
    "trend",
    "spot",
    "derivatives",
    "orderflow",
    "liquidations",
    "options",
    "onchain",
    "whales",
    "etf",
    "macro",
    "sentiment",
    "historical_twin",
    "source_quality",
    "risk",
]

DECISIONS = [
    "LONG",
    "SHORT",
    "NO_TRADE",
]

# Risk Engine
MIN_CONFIDENCE_TO_TRADE = 70
MIN_RR_TO_TRADE = 1.8

# Data collection
SOURCE_TIMEOUT_SECONDS = 12
MAX_SOURCE_RETRIES = 2

# ORACLE cycles
SNAPSHOT_INTERVAL_SECONDS = 300       # 5 хв
SIGNAL_RECHECK_INTERVAL_SECONDS = 60  # 1 хв

# Безпека: поки тільки paper trading
PAPER_TRADING_ENABLED = True
LIVE_TRADING_ENABLED = False