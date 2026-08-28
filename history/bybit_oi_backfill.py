import sqlite3
import time
from datetime import datetime, timezone, timedelta

import requests

from config.settings import DB_PATH


BASE_URL = "https://api.bybit.com"
SYMBOL_API = "BTCUSDT"
SYMBOL_DB = "BTC"
SOURCE = "bybit_futures"

INTERVAL = "1h"
CHUNK_DAYS = 7
TIMEOUT = 20

session = requests.Session()
session.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


def fetch_oi_chunk(start_ms, end_ms):
    response = session.get(
        BASE_URL + "/v5/market/open-interest",
        params={
            "category": "linear",
            "symbol": SYMBOL_API,
            "intervalTime": INTERVAL,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": 200,
        },
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    data = response.json()

    if data.get("retCode") != 0:
        raise RuntimeError(
            f"Bybit error: {data.get('retCode')} "
            f"{data.get('retMsg')}"
        )

    return data.get("result", {}).get("list", [])


def save_oi_row(con, row):
    ts_ms = int(row["timestamp"])
    ts_unix = ts_ms // 1000

    timestamp = datetime.fromtimestamp(
        ts_unix,
        tz=timezone.utc,
    ).isoformat()

    open_interest = float(row["openInterest"])

    con.execute(
        """
        INSERT INTO derivatives_history (
            timestamp,
            timestamp_unix,
            symbol,
            source,
            open_interest
        )
        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(symbol, source, timestamp_unix)
        DO UPDATE SET
            open_interest = excluded.open_interest,
            timestamp = excluded.timestamp
        """,
        (
            timestamp,
            ts_unix,
            SYMBOL_DB,
            SOURCE,
            open_interest,
        ),
    )


def backfill_oi(days=180):
    now = datetime.now(timezone.utc)

    start = now - timedelta(days=days)

    print()
    print("ORACLE X - BYBIT OPEN INTEREST BACKFILL")
    print(f"Symbol: {SYMBOL_DB}")
    print(f"Source: {SOURCE}")
    print(f"Interval: {INTERVAL}")
    print(f"Period: {days} days")
    print()

    con = sqlite3.connect(DB_PATH)

    total_received = 0
    current_start = start

    while current_start < now:
        current_end = min(
            current_start + timedelta(days=CHUNK_DAYS),
            now,
        )

        start_ms = int(current_start.timestamp() * 1000)
        end_ms = int(current_end.timestamp() * 1000)

        rows = fetch_oi_chunk(
            start_ms,
            end_ms,
        )

        if rows:
            rows = sorted(
                rows,
                key=lambda x: int(x["timestamp"]),
            )

            for row in rows:
                save_oi_row(con, row)

            con.commit()

            total_received += len(rows)

            first_ts = int(rows[0]["timestamp"])
            last_ts = int(rows[-1]["timestamp"])

            first = datetime.fromtimestamp(
                first_ts / 1000,
                tz=timezone.utc,
            )

            last = datetime.fromtimestamp(
                last_ts / 1000,
                tz=timezone.utc,
            )

            print(
                f"Received: {len(rows):3} | "
                f"{first} -> {last}"
            )
        else:
            print(
                f"Received: 0 | "
                f"{current_start} -> {current_end}"
            )

        current_start = current_end

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
            SYMBOL_DB,
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
            SYMBOL_DB,
            SOURCE,
        ),
    ).fetchone()

    con.close()

    print()
    print(f"API rows received: {total_received}")
    print(f"OI rows in DB: {count}")
    print(f"First: {first_last[0]}")
    print(f"Last:  {first_last[1]}")
    print()
    print("BYBIT OI BACKFILL COMPLETE")


if __name__ == "__main__":
    backfill_oi(days=180)
