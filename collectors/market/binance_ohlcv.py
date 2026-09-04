import json
import sqlite3
import time
from datetime import datetime, timezone

import requests

from config.settings import DB_PATH


BASE_URL = "https://api.binance.com"
BINANCE_SYMBOL = "BTCUSDT"
SYMBOL = "BTC"
SOURCE = "Binance"

TIMEFRAMES = ("5m", "15m", "1h", "4h")

TIMEOUT = 15
POLL_SECONDS = 60

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


def get_json(path, params=None):
    response = SESSION.get(
        BASE_URL + path,
        params=params,
        timeout=TIMEOUT,
    )

    response.raise_for_status()
    return response.json()


def fetch_klines(timeframe: str, limit: int = 3):
    return get_json(
        "/api/v3/klines",
        {
            "symbol": BINANCE_SYMBOL,
            "interval": timeframe,
            "limit": limit,
        },
    )


def save_candle(timeframe: str, row):
    open_time_ms = int(row[0])
    close_time_ms = int(row[6])

    now_ms = int(time.time() * 1000)

    # Не зберігаємо ще незакриту свічку.
    if close_time_ms > now_ms:
        return False

    timestamp_unix = open_time_ms // 1000

    timestamp = datetime.fromtimestamp(
        timestamp_unix,
        tz=timezone.utc,
    ).isoformat()

    open_price = float(row[1])
    high = float(row[2])
    low = float(row[3])
    close = float(row[4])
    volume = float(row[5])

    raw = {
        "open_time_ms": open_time_ms,
        "close_time_ms": close_time_ms,
        "quote_volume": float(row[7]),
        "trades": int(row[8]),
        "taker_buy_base_volume": float(row[9]),
        "taker_buy_quote_volume": float(row[10]),
    }

    con = sqlite3.connect(DB_PATH)

    try:
        existing = con.execute(
            """
            SELECT id
            FROM market_snapshots
            WHERE symbol = ?
              AND timeframe = ?
              AND timestamp_unix = ?
            LIMIT 1
            """,
            (
                SYMBOL,
                timeframe,
                timestamp_unix,
            ),
        ).fetchone()

        if existing:
            con.execute(
                """
                UPDATE market_snapshots
                SET
                    timestamp = ?,
                    source = ?,
                    open = ?,
                    high = ?,
                    low = ?,
                    close = ?,
                    volume = ?,
                    raw_json = ?
                WHERE id = ?
                """,
                (
                    timestamp,
                    SOURCE,
                    open_price,
                    high,
                    low,
                    close,
                    volume,
                    json.dumps(
                        raw,
                        ensure_ascii=False,
                    ),
                    existing[0],
                ),
            )

            action = "UPDATED"

        else:
            con.execute(
                """
                INSERT INTO market_snapshots (
                    timestamp,
                    timestamp_unix,
                    symbol,
                    source,
                    timeframe,
                    open,
                    high,
                    low,
                    close,
                    volume,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    timestamp_unix,
                    SYMBOL,
                    SOURCE,
                    timeframe,
                    open_price,
                    high,
                    low,
                    close,
                    volume,
                    json.dumps(
                        raw,
                        ensure_ascii=False,
                    ),
                ),
            )

            action = "INSERTED"

        con.commit()

    finally:
        con.close()

    print(
        f"{action} | "
        f"{timeframe} | "
        f"{timestamp} | "
        f"CLOSE={close}",
        flush=True,
    )

    return True


def collect_once():
    for timeframe in TIMEFRAMES:
        try:
            rows = fetch_klines(
                timeframe=timeframe,
                limit=3,
            )

            # Зберігаємо останні закриті свічки.
            for row in rows:
                save_candle(
                    timeframe,
                    row,
                )

        except Exception as exc:
            print(
                f"{timeframe} ERROR:",
                exc,
                flush=True,
            )


def main():
    print()
    print(
        "ORACLE X - BINANCE OHLCV",
        flush=True,
    )
    print(
        "Mode: REST closed candles",
        flush=True,
    )
    print(
        "Timeframes:",
        ", ".join(TIMEFRAMES),
        flush=True,
    )
    print(
        "Status: LIVE",
        flush=True,
    )
    print()

    while True:
        collect_once()
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
