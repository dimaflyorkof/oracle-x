import sqlite3
import time
from datetime import datetime, timedelta, timezone

import requests

from config.settings import DB_PATH


SYMBOL = "BTC"
INST_ID = "BTC-USDT-SWAP"
SOURCE = "okx_futures"

BASE_URL = "https://www.okx.com"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


def get_json(path, params=None):
    response = SESSION.get(
        BASE_URL + path,
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()

    if payload.get("code") != "0":
        raise RuntimeError(
            f"OKX error: {payload.get('code')} "
            f"{payload.get('msg')}"
        )

    return payload.get("data", [])


def fetch_funding_page(after=None, limit=100):
    params = {
        "instId": INST_ID,
        "limit": str(limit),
    }

    if after is not None:
        params["after"] = str(after)

    return get_json(
        "/api/v5/public/funding-rate-history",
        params=params,
    )


def save_row(con, row):
    ts_ms = int(row["fundingTime"])
    ts_unix = ts_ms // 1000

    dt = datetime.fromtimestamp(
        ts_unix,
        tz=timezone.utc,
    )

    funding_rate = float(row["fundingRate"])

    con.execute(
        """
        INSERT INTO derivatives_history (
            timestamp,
            timestamp_unix,
            symbol,
            source,
            funding_rate,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?)

        ON CONFLICT(symbol, source, timestamp_unix)
        DO UPDATE SET
            funding_rate = excluded.funding_rate,
            raw_json = excluded.raw_json
        """,
        (
            dt.isoformat(),
            ts_unix,
            SYMBOL,
            SOURCE,
            funding_rate,
            str(row),
        ),
    )

    return ts_ms, dt


def backfill(days=180):
    con = sqlite3.connect(DB_PATH)

    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=days)

    start_ms = int(start_dt.timestamp() * 1000)

    after = None
    total_saved = 0

    print()
    print("ORACLE X - OKX FUNDING BACKFILL")
    print("Symbol:", SYMBOL)
    print("Instrument:", INST_ID)
    print("Source:", SOURCE)
    print("History:", days, "days")
    print()

    while True:
        rows = fetch_funding_page(
            after=after,
            limit=100,
        )

        if not rows:
            break

        oldest_ts = None
        oldest_dt = None
        saved_this_page = 0

        for row in rows:
            try:
                ts_ms = int(row["fundingTime"])
            except (KeyError, TypeError, ValueError):
                continue

            if oldest_ts is None or ts_ms < oldest_ts:
                oldest_ts = ts_ms
                oldest_dt = datetime.fromtimestamp(
                    ts_ms / 1000,
                    tz=timezone.utc,
                )

            if ts_ms < start_ms:
                continue

            save_row(con, row)

            total_saved += 1
            saved_this_page += 1

        con.commit()

        print(
            f"page={len(rows)} | "
            f"saved={saved_this_page} | "
            f"oldest={oldest_dt.isoformat() if oldest_dt else 'N/A'}"
        )

        if oldest_ts is None:
            break

        if oldest_ts <= start_ms:
            break

        next_after = oldest_ts

        if after is not None and next_after >= after:
            print("Pagination stopped: cursor did not move backward")
            break

        after = next_after

        time.sleep(0.15)

    row = con.execute(
        """
        SELECT
            COUNT(*),
            MIN(timestamp),
            MAX(timestamp)
        FROM derivatives_history
        WHERE symbol = ?
          AND source = ?
          AND funding_rate IS NOT NULL
        """,
        (
            SYMBOL,
            SOURCE,
        ),
    ).fetchone()

    con.close()

    print()
    print("Saved this run:", total_saved)
    print("DB rows:", row[0])
    print("First:", row[1])
    print("Last:", row[2])
    print()
    print("OKX FUNDING BACKFILL COMPLETE")


if __name__ == "__main__":
    backfill(days=180)
