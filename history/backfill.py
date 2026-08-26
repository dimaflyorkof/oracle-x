import time
import requests
from datetime import datetime, timedelta, timezone

from config.settings import (
    DEFAULT_SYMBOLS,
    HISTORY_DAYS_CORE,
    SOURCE_TIMEOUT_SECONDS,
)

from database.db import (
    init_database,
    insert_json,
)


BINANCE_BASE_URL = "https://api.binance.com"

BINANCE_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "BNB": "BNBUSDT",
    "XRP": "XRPUSDT",
    "ADA": "ADAUSDT",
    "DOGE": "DOGEUSDT",
    "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT",
    "TON": "TONUSDT",
}

TIMEFRAME_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "ORACLE-X-CORE/1.0",
    "Accept": "application/json",
})


def to_milliseconds(dt):
    return int(dt.timestamp() * 1000)


def utc_now():
    return datetime.now(timezone.utc)


def fetch_binance_klines(
    symbol,
    interval,
    start_ms,
    end_ms,
    limit=1000,
):
    """
    Завантажує один batch історичних свічок Binance.
    """

    url = f"{BINANCE_BASE_URL}/api/v3/klines"

    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": limit,
    }

    response = SESSION.get(
        url,
        params=params,
        timeout=SOURCE_TIMEOUT_SECONDS,
    )

    response.raise_for_status()

    return response.json()


def save_candle(
    logical_symbol,
    timeframe,
    source,
    candle,
):
    """
    Записує одну свічку у market_snapshots.
    """

    open_time = int(candle[0])

    open_price = float(candle[1])
    high_price = float(candle[2])
    low_price = float(candle[3])
    close_price = float(candle[4])
    volume = float(candle[5])

    timestamp_iso = datetime.fromtimestamp(
        open_time / 1000,
        tz=timezone.utc,
    ).isoformat()

    data = {
        "timestamp": timestamp_iso,
        "timestamp_unix": int(open_time / 1000),

        "symbol": logical_symbol,
        "timeframe": timeframe,

        "price": close_price,

        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,

        "volume": volume,

        "source": source,
        "source_quality": 100.0,

        "raw_json": {
            "open_time": candle[0],
            "close_time": candle[6],
            "quote_volume": candle[7],
            "trades": candle[8],
            "taker_buy_base": candle[9],
            "taker_buy_quote": candle[10],
        },
    }

    insert_json(
        "market_snapshots",
        data,
    )


def backfill_symbol_timeframe(
    logical_symbol,
    timeframe,
    days,
):
    """
    Завантажує всю історію для однієї монети
    та одного timeframe.
    """

    exchange_symbol = BINANCE_SYMBOLS.get(
        logical_symbol
    )

    if not exchange_symbol:
        print(
            f"SKIP {logical_symbol}: "
            f"Binance symbol not configured"
        )
        return

    if timeframe not in TIMEFRAME_MS:
        raise ValueError(
            f"Unsupported timeframe: {timeframe}"
        )

    end_dt = utc_now()

    start_dt = end_dt - timedelta(
        days=days
    )

    start_ms = to_milliseconds(
        start_dt
    )

    final_end_ms = to_milliseconds(
        end_dt
    )

    step_ms = TIMEFRAME_MS[timeframe]

    total_saved = 0

    print(
        f"\nBACKFILL {logical_symbol} "
        f"{timeframe} — {days} days"
    )

    while start_ms < final_end_ms:

        try:
            candles = fetch_binance_klines(
                exchange_symbol,
                timeframe,
                start_ms,
                final_end_ms,
                limit=1000,
            )

        except requests.RequestException as exc:

            print(
                f"ERROR {logical_symbol} "
                f"{timeframe}: {exc}"
            )

            print(
                "Retry in 5 seconds..."
            )

            time.sleep(5)

            continue

        if not candles:
            break

        for candle in candles:

            save_candle(
                logical_symbol,
                timeframe,
                "Binance",
                candle,
            )

            total_saved += 1

        last_open_time = int(
            candles[-1][0]
        )

        next_start = (
            last_open_time
            + step_ms
        )

        if next_start <= start_ms:
            break

        start_ms = next_start

        print(
            f"{logical_symbol} "
            f"{timeframe}: "
            f"{total_saved} candles saved"
        )

        # невелика пауза, щоб не бити API
        time.sleep(0.15)

    print(
        f"DONE {logical_symbol} "
        f"{timeframe}: "
        f"{total_saved} candles"
    )


def backfill_core_history(
    symbols=None,
    days=HISTORY_DAYS_CORE,
):
    """
    Основний backfill ORACLE X.

    За замовчуванням:
    10 монет
    5 timeframe
    180 днів
    """

    if symbols is None:
        symbols = DEFAULT_SYMBOLS

    timeframes = [
        "5m",
        "15m",
        "1h",
        "4h",
        "1d",
    ]

    init_database()

    print(
        "\n================================"
    )

    print(
        "ORACLE X HISTORICAL BACKFILL"
    )

    print(
        "================================"
    )

    print(
        f"Days: {days}"
    )

    print(
        f"Symbols: {len(symbols)}"
    )

    print(
        f"Timeframes: {len(timeframes)}"
    )

    for symbol in symbols:

        for timeframe in timeframes:

            backfill_symbol_timeframe(
                symbol,
                timeframe,
                days,
            )

    print(
        "\n================================"
    )

    print(
        "ORACLE X BACKFILL COMPLETE"
    )

    print(
        "================================"
    )


def test_backfill():
    """
    Безпечний тест:
    тільки BTC
    тільки 1h
    тільки останні 2 дні.
    """

    init_database()

    backfill_symbol_timeframe(
        logical_symbol="BTC",
        timeframe="1h",
        days=2,
    )


if __name__ == "__main__":
    backfill_core_history(
        symbols=["BTC"],
        days=180,
    )