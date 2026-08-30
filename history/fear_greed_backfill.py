import json
import sqlite3
from datetime import datetime, timezone

import requests

from config.settings import DB_PATH


SOURCE = "alternative_me"
SYMBOL = "BTC"

URL = "https://api.alternative.me/fng/"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


def fetch_history(limit=180):
    response = SESSION.get(
        URL,
        params={
            "limit": limit,
            "format": "json",
        },
        timeout=30,
    )

    response.raise_for_status()

    payload = response.json()

    return payload.get("data", [])


def backfill(limit=180):
    rows = fetch_history(limit=limit)

    con = sqlite3.connect(DB_PATH)

    saved = 0

    print()
    print("ORACLE X - FEAR & GREED BACKFILL")
    print("Source:", SOURCE)
    print("Requested:", limit)
    print()

    for row in rows:
        try:
            value = float(row["value"])
            ts_unix = int(row["timestamp"])

            dt = datetime.fromtimestamp(
                ts_unix,
                tz=timezone.utc,
            )

            classification = row.get(
                "value_classification"
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            continue

        # не дублюємо однаковий день/джерело
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
            continue

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

        saved += 1

    con.commit()

    result = con.execute(
        """
        SELECT
            COUNT(*),
            MIN(timestamp),
            MAX(timestamp),
            MIN(sentiment_score),
            MAX(sentiment_score)
        FROM sentiment_history
        WHERE source = ?
          AND symbol = ?
        """,
        (
            SOURCE,
            SYMBOL,
        ),
    ).fetchone()

    con.close()

    print("Saved this run:", saved)
    print("DB rows:", result[0])
    print("First:", result[1])
    print("Last:", result[2])
    print("Min score:", result[3])
    print("Max score:", result[4])
    print()
    print("FEAR & GREED BACKFILL COMPLETE")


if __name__ == "__main__":
    backfill(limit=180)
