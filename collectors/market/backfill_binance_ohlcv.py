import sqlite3
import time
from datetime import datetime, timezone

import requests

from config.settings import DB_PATH


BASE_URL = "https://api.binance.com"
BINANCE_SYMBOL = "BTCUSDT"
SYMBOL = "BTC"
SOURCE = "Binance"

TIMEFRAMES = {
    "5m": 300,
    "15m": 900,
    "1h": 3600,
    "4h": 14400,
}

TIMEOUT = 15
LIMIT = 1000

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


def get_json(path, params=None):
    r = SESSION.get(
        BASE_URL + path,
        params=params,
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def latest_existing_ts(con, timeframe):
    row = con.execute(
        """
        SELECT MAX(timestamp_unix)
        FROM market_snapshots
        WHERE symbol = ?
          AND timeframe = ?
          AND timestamp_unix < ?
        """,
        (
            SYMBOL,
            timeframe,
            1788360000,  # before Sep 2 live inserts
        ),
    ).fetchone()

    return int(row[0]) if row and row[0] else None


def save_row(con, timeframe, row):
    open_time_ms = int(row[0])
    timestamp_unix = open_time_ms // 1000

    timestamp = datetime.fromtimestamp(
        timestamp_unix,
        tz=timezone.utc,
    ).isoformat()

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

    values = (
        timestamp,
        timestamp_unix,
        SYMBOL,
        SOURCE,
        timeframe,
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        float(row[5]),
    )

    if existing:
        con.execute(
            """
            UPDATE market_snapshots
            SET timestamp = ?,
                source = ?,
                open = ?,
                high = ?,
                low = ?,
                close = ?,
                volume = ?
            WHERE id = ?
            """,
            (
                timestamp,
                SOURCE,
                float(row[1]),
                float(row[2]),
                float(row[3]),
                float(row[4]),
                float(row[5]),
                existing[0],
            ),
        )
        return "UPDATED"

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
            volume
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )

    return "INSERTED"


def backfill_timeframe(timeframe):
    con = sqlite3.connect(DB_PATH)

    try:
        last_ts = latest_existing_ts(con, timeframe)

        if last_ts is None:
            print(timeframe, "NO START POINT")
            return

        step = TIMEFRAMES[timeframe]
        start_ms = (last_ts + step) * 1000

        total = 0

        while True:
            rows = get_json(
                "/api/v3/klines",
                {
                    "symbol": BINANCE_SYMBOL,
                    "interval": timeframe,
                    "startTime": start_ms,
                    "limit": LIMIT,
                },
            )

            if not rows:
                break

            now_ms = int(time.time() * 1000)

            saved_this_page = 0

            for row in rows:
                close_time_ms = int(row[6])

                if close_time_ms > now_ms:
                    continue

                save_row(
                    con,
                    timeframe,
                    row,
                )

                total += 1
                saved_this_page += 1

            con.commit()

            last_open_ms = int(rows[-1][0])
            next_start_ms = last_open_ms + step * 1000

            if next_start_ms <= start_ms:
                break

            start_ms = next_start_ms

            if len(rows) < LIMIT:
                break

            time.sleep(0.2)

        print(
            f"{timeframe}: backfilled {total} candles",
            flush=True,
        )

    finally:
        con.close()


def main():
    print()
    print("ORACLE X — BINANCE OHLCV BACKFILL")
    print("=" * 60)

    for timeframe in ("5m", "15m", "1h", "4h"):
        backfill_timeframe(timeframe)


if __name__ == "__main__":
    main()
