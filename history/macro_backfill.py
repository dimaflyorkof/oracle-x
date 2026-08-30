import json
import sqlite3
from datetime import datetime, timezone

import requests

from config.settings import DB_PATH


SOURCE = "yahoo_finance"

SERIES = {
    "dxy": "DX-Y.NYB",
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "vix": "^VIX",
    "us10y": "^TNX",
    "gold": "GC=F",
}

BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
})


def fetch_series(symbol):
    response = SESSION.get(
        f"{BASE_URL}/{symbol}",
        params={
            "range": "6mo",
            "interval": "1d",
            "events": "history",
        },
        timeout=60,
    )

    response.raise_for_status()

    payload = response.json()

    chart = payload.get("chart", {})
    result = chart.get("result")

    if not result:
        return []

    data = result[0]

    timestamps = data.get("timestamp") or []

    indicators = data.get("indicators", {})
    quotes = indicators.get("quote") or []

    if not quotes:
        return []

    closes = quotes[0].get("close") or []

    rows = []

    for ts_unix, close_value in zip(
        timestamps,
        closes,
    ):
        if close_value is None:
            continue

        try:
            ts_unix = int(ts_unix)
            close_value = float(close_value)

        except (
            TypeError,
            ValueError,
        ):
            continue

        dt = datetime.fromtimestamp(
            ts_unix,
            tz=timezone.utc,
        )

        day_dt = datetime(
            dt.year,
            dt.month,
            dt.day,
            tzinfo=timezone.utc,
        )

        rows.append({
            "timestamp": day_dt.isoformat(),
            "timestamp_unix": int(
                day_dt.timestamp()
            ),
            "value": close_value,
        })

    return rows


def load_all():
    combined = {}

    for field, symbol in SERIES.items():
        print(
            "Downloading:",
            field,
            symbol,
            flush=True,
        )

        try:
            rows = fetch_series(symbol)

        except Exception as exc:
            print(
                "ERROR:",
                field,
                repr(exc),
                flush=True,
            )
            continue

        print(
            "Rows:",
            len(rows),
            flush=True,
        )

        for row in rows:
            ts_unix = row["timestamp_unix"]

            combined.setdefault(
                ts_unix,
                {
                    "timestamp": row["timestamp"],
                },
            )

            combined[ts_unix][field] = row["value"]

    return combined


def backfill():
    print()
    print(
        "ORACLE X - MACRO BACKFILL",
        flush=True,
    )
    print(
        "Source:",
        SOURCE,
        flush=True,
    )
    print()

    data = load_all()

    con = sqlite3.connect(DB_PATH)

    saved = 0
    updated = 0

    for ts_unix in sorted(data):
        values = data[ts_unix]

        existing = con.execute(
            """
            SELECT id
            FROM macro_history
            WHERE timestamp_unix = ?
            LIMIT 1
            """,
            (
                ts_unix,
            ),
        ).fetchone()

        raw = {
            "source": SOURCE,
            **values,
        }

        if existing:
            con.execute(
                """
                UPDATE macro_history
                SET
                    dxy = COALESCE(?, dxy),
                    nasdaq = COALESCE(?, nasdaq),
                    sp500 = COALESCE(?, sp500),
                    vix = COALESCE(?, vix),
                    us10y = COALESCE(?, us10y),
                    gold = COALESCE(?, gold),
                    raw_json = ?
                WHERE id = ?
                """,
                (
                    values.get("dxy"),
                    values.get("nasdaq"),
                    values.get("sp500"),
                    values.get("vix"),
                    values.get("us10y"),
                    values.get("gold"),
                    json.dumps(
                        raw,
                        ensure_ascii=False,
                    ),
                    existing[0],
                ),
            )

            updated += 1

        else:
            con.execute(
                """
                INSERT INTO macro_history (
                    timestamp,
                    timestamp_unix,
                    dxy,
                    nasdaq,
                    sp500,
                    vix,
                    us10y,
                    gold,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["timestamp"],
                    ts_unix,
                    values.get("dxy"),
                    values.get("nasdaq"),
                    values.get("sp500"),
                    values.get("vix"),
                    values.get("us10y"),
                    values.get("gold"),
                    json.dumps(
                        raw,
                        ensure_ascii=False,
                    ),
                ),
            )

            saved += 1

    con.commit()

    result = con.execute(
        """
        SELECT
            COUNT(*),
            MIN(timestamp),
            MAX(timestamp),
            COUNT(dxy),
            COUNT(nasdaq),
            COUNT(sp500),
            COUNT(vix),
            COUNT(us10y),
            COUNT(gold)
        FROM macro_history
        """
    ).fetchone()

    con.close()

    print()
    print("Saved:", saved)
    print("Updated:", updated)
    print("DB rows:", result[0])
    print("First:", result[1])
    print("Last:", result[2])
    print("DXY rows:", result[3])
    print("NASDAQ rows:", result[4])
    print("S&P 500 rows:", result[5])
    print("VIX rows:", result[6])
    print("US10Y rows:", result[7])
    print("Gold rows:", result[8])
    print()
    print(
        "MACRO BACKFILL COMPLETE",
        flush=True,
    )


if __name__ == "__main__":
    backfill()
