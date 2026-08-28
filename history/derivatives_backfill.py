import sqlite3
import time
from datetime import datetime, timezone, timedelta

import requests

from config.settings import DB_PATH


BASE_URL = "https://fapi.binance.com"
SYMBOL_API = "BTCUSDT"
SYMBOL_DB = "BTC"
SOURCE = "binance_futures"

TIMEOUT = 20
LIMIT = 1000

session = requests.Session()
session.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


def fetch_funding_page(start_ms, end_ms):
    response = session.get(
        BASE_URL + "/fapi/v1/fundingRate",
        params={
            "symbol": SYMBOL_API,
            "startTime": start_ms,
            "endTime": end_ms,
            "limit": LIMIT,
        },
        timeout=TIMEOUT,
    )

    response.raise_for_status()
    return response.json()


def save_funding_row(con, row):
    ts_ms = int(row["fundingTime"])
    ts_unix = ts_ms // 1000

    timestamp = datetime.fromtimestamp(
        ts_unix,
        tz=timezone.utc,
    ).isoformat()

    funding_rate = float(row["fundingRate"])

    con.execute(
        """
        INSERT INTO derivatives_history (
            timestamp,
            timestamp_unix,
            symbol,
            source,
            funding_rate
        )
        VALUES (?, ?, ?, ?, ?)

        ON CONFLICT(symbol, source, timestamp_unix)
        DO UPDATE SET
            funding_rate = excluded.funding_rate,
            timestamp = excluded.timestamp
        """,
        (
            timestamp,
            ts_unix,
            SYMBOL_DB,
            SOURCE,
            funding_rate,
        ),
    )


def backfill_funding(days=3):
    now = datetime.now(timezone.utc)

    start = now - timedelta(days=days)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    print()
    print("ORACLE X - FUNDING BACKFILL TEST")
    print(f"Symbol: {SYMBOL_DB}")
    print(f"Source: {SOURCE}")
    print(f"Period: {days} days")
    print()

    con = sqlite3.connect(DB_PATH)

    total_received = 0

    while start_ms <= end_ms:
        data = fetch_funding_page(
            start_ms,
            end_ms,
        )

        if not data:
            break

        for row in data:
            save_funding_row(con, row)

        con.commit()

        total_received += len(data)

        first_ts = int(data[0]["fundingTime"])
        last_ts = int(data[-1]["fundingTime"])

        print(
            f"Received: {len(data)} | "
            f"{datetime.fromtimestamp(first_ts / 1000, tz=timezone.utc)} "
            f"-> "
            f"{datetime.fromtimestamp(last_ts / 1000, tz=timezone.utc)}"
        )

        if len(data) < LIMIT:
            break

        next_start = last_ts + 1

        if next_start <= start_ms:
            break

        start_ms = next_start

        time.sleep(0.2)

    count = con.execute(
        """
        SELECT COUNT(*)
        FROM derivatives_history
        WHERE symbol = ?
          AND source = ?
          AND funding_rate IS NOT NULL
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
          AND funding_rate IS NOT NULL
        """,
        (
            SYMBOL_DB,
            SOURCE,
        ),
    ).fetchone()

    con.close()

    print()
    print(f"API rows received: {total_received}")
    print(f"Funding rows in DB: {count}")
    print(f"First: {first_last[0]}")
    print(f"Last:  {first_last[1]}")
    print()
    print("FUNDING TEST COMPLETE")


if __name__ == "__main__":
    backfill_funding(days=3)
