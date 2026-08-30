import json
import sqlite3
import time
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

POLL_SECONDS = 3600

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
})


def fetch_latest(symbol):
    response = SESSION.get(
        f"{BASE_URL}/{symbol}",
        params={
            "range": "5d",
            "interval": "1d",
            "events": "history",
        },
        timeout=60,
    )

    response.raise_for_status()

    payload = response.json()

    result = payload.get(
        "chart",
        {},
    ).get(
        "result"
    )

    if not result:
        return None

    data = result[0]

    timestamps = data.get(
        "timestamp"
    ) or []

    quotes = data.get(
        "indicators",
        {},
    ).get(
        "quote"
    ) or []

    if not timestamps or not quotes:
        return None

    closes = quotes[0].get(
        "close"
    ) or []

    for ts_unix, close_value in reversed(
        list(
            zip(
                timestamps,
                closes,
            )
        )
    ):
        if close_value is None:
            continue

        dt = datetime.fromtimestamp(
            int(ts_unix),
            tz=timezone.utc,
        )

        day_dt = datetime(
            dt.year,
            dt.month,
            dt.day,
            tzinfo=timezone.utc,
        )

        return {
            "timestamp": day_dt.isoformat(),
            "timestamp_unix": int(
                day_dt.timestamp()
            ),
            "value": float(close_value),
        }

    return None


def fetch_all():
    combined = {}

    for field, symbol in SERIES.items():
        try:
            row = fetch_latest(symbol)

        except Exception as exc:
            print(
                "ERROR:",
                field,
                repr(exc),
                flush=True,
            )
            continue

        if row is None:
            continue

        ts_unix = row[
            "timestamp_unix"
        ]

        combined.setdefault(
            ts_unix,
            {
                "timestamp": row[
                    "timestamp"
                ],
            },
        )

        combined[
            ts_unix
        ][field] = row["value"]

    return combined


def save_data(data):
    if not data:
        print(
            "No macro data received",
            flush=True,
        )
        return

    con = sqlite3.connect(DB_PATH)

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

            print(
                "Updated:",
                values["timestamp"],
                values,
                flush=True,
            )

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

            print(
                "Saved:",
                values["timestamp"],
                values,
                flush=True,
            )

    con.commit()
    con.close()


def run():
    print()
    print(
        "ORACLE X - YAHOO MACRO LIVE",
        flush=True,
    )
    print(
        "Source:",
        SOURCE,
        flush=True,
    )
    print(
        "Poll:",
        POLL_SECONDS,
        "seconds",
        flush=True,
    )
    print(
        "Status: LIVE",
        flush=True,
    )
    print()

    while True:
        try:
            data = fetch_all()

            save_data(data)

        except KeyboardInterrupt:
            print(
                "\nCollector stopped",
                flush=True,
            )
            break

        except Exception as exc:
            print(
                "ERROR:",
                repr(exc),
                flush=True,
            )

        time.sleep(
            POLL_SECONDS
        )


if __name__ == "__main__":
    run()
