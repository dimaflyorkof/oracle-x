import sqlite3
import time
from datetime import datetime, timedelta, timezone

import requests

from config.settings import DB_PATH


SYMBOL = "BTC"
BINANCE_SYMBOL = "BTCUSDT"
SOURCE = "binance_futures"
PERIOD = "1h"


def to_iso(ts_ms):
    return datetime.fromtimestamp(
        ts_ms / 1000,
        tz=timezone.utc,
    ).isoformat()


def fetch_chunk(start_ms, end_ms):
    url = "https://fapi.binance.com/futures/data/openInterestHist"

    params = {
        "symbol": BINANCE_SYMBOL,
        "period": PERIOD,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 500,
    }

    r = requests.get(
        url,
        params=params,
        timeout=20,
    )

    r.raise_for_status()

    data = r.json()

    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected Binance response: {data}")

    return data


def save_row(con, row):
    ts_ms = int(row["timestamp"])
    ts_unix = ts_ms // 1000

    open_interest = float(row["sumOpenInterest"])

    con.execute(
        """
        INSERT INTO derivatives_history (
            timestamp,
            timestamp_unix,
            symbol,
            source,
            open_interest,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, source, timestamp_unix)
        DO UPDATE SET
            open_interest = excluded.open_interest
        """,
        (
            to_iso(ts_ms),
            ts_unix,
            SYMBOL,
            SOURCE,
            open_interest,
            str(row),
        ),
    )


def backfill_oi(days=180):
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    cursor = start

    total_received = 0

    con = sqlite3.connect(DB_PATH)

    print()
    print("ORACLE X - BINANCE OI BACKFILL")
    print("Symbol:", SYMBOL)
    print("Source:", SOURCE)
    print("Interval:", PERIOD)
    print("History:", days, "days")
    print()

    while cursor < now:
        chunk_end = min(
            cursor + timedelta(days=20),
            now,
        )

        start_ms = int(cursor.timestamp() * 1000)
        end_ms = int(chunk_end.timestamp() * 1000)

        rows = fetch_chunk(
            start_ms,
            end_ms,
        )

        if rows:
            first = to_iso(
                int(rows[0]["timestamp"])
            )
            last = to_iso(
                int(rows[-1]["timestamp"])
            )

            print(
                f"Received: {len(rows)} | "
                f"{first} -> {last}"
            )

            for row in rows:
                save_row(con, row)

            con.commit()

            total_received += len(rows)

        cursor = chunk_end

        time.sleep(0.15)

    count = con.execute(
        """
        SELECT COUNT(*)
        FROM derivatives_history
        WHERE symbol = ?
          AND source = ?
          AND open_interest IS NOT NULL
        """,
        (
            SYMBOL,
            SOURCE,
        ),
    ).fetchone()[0]

    first_last = con.execute(
        """
        SELECT
            MIN(timestamp),
            MAX(timestamp)
        FROM derivatives_history
        WHERE symbol = ?
          AND source = ?
          AND open_interest IS NOT NULL
        """,
        (
            SYMBOL,
            SOURCE,
        ),
    ).fetchone()

    con.close()

    print()
    print("API rows received:", total_received)
    print("OI rows in DB:", count)
    print("First:", first_last[0])
    print("Last:", first_last[1])
    print()
    print("BINANCE OI BACKFILL COMPLETE")


if __name__ == "__main__":
    backfill_oi(days=30)
