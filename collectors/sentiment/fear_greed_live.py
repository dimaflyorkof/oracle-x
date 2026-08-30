import json
import sqlite3
import time
from datetime import datetime, timezone

import requests

from config.settings import DB_PATH


SOURCE = "alternative_me"
SYMBOL = "BTC"

URL = "https://api.alternative.me/fng/"

POLL_SECONDS = 3600

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


def fetch_latest():
    response = SESSION.get(
        URL,
        params={
            "limit": 1,
            "format": "json",
        },
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()
    rows = payload.get("data", [])

    if not rows:
        return None

    return rows[0]


def save_latest(row):
    value = float(row["value"])
    ts_unix = int(row["timestamp"])

    classification = row.get(
        "value_classification"
    )

    dt = datetime.fromtimestamp(
        ts_unix,
        tz=timezone.utc,
    )

    con = sqlite3.connect(DB_PATH)

    existing = con.execute(
        """
        SELECT 1
        FROM sentiment_history
        WHERE timestamp_unix = ?
          AND source = ?
          AND symbol = ?
        LIMIT 1
        """,
        (
            ts_unix,
            SOURCE,
            SYMBOL,
        ),
    ).fetchone()

    if existing:
        con.close()

        print(
            "Already saved:",
            dt.isoformat(),
            value,
            classification,
            flush=True,
        )

        return False

    con.execute(
        """
        INSERT INTO sentiment_history (
            timestamp,
            timestamp_unix,
            symbol,
            source,
            sentiment_score,
            headline,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            dt.isoformat(),
            ts_unix,
            SYMBOL,
            SOURCE,
            value,
            classification,
            json.dumps(
                row,
                ensure_ascii=False,
            ),
        ),
    )

    con.commit()
    con.close()

    print(
        "Saved:",
        dt.isoformat(),
        "| score=",
        value,
        "|",
        classification,
        flush=True,
    )

    return True


def run():
    print()
    print(
        "ORACLE X - FEAR & GREED LIVE",
        flush=True,
    )
    print(
        "Source:",
        SOURCE,
        flush=True,
    )
    print(
        "Symbol:",
        SYMBOL,
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
            row = fetch_latest()

            if row is not None:
                save_latest(row)

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

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
