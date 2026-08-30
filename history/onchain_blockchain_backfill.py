import json
import sqlite3
from datetime import datetime, timezone

import requests

from config.settings import DB_PATH


SOURCE = "blockchain_com"
SYMBOL = "BTC"

BASE_URL = "https://api.blockchain.info/charts"

CHARTS = {
    "hash_rate": "hash-rate",
    "tx_count": "n-transactions",
    "tx_volume_usd": "estimated-transaction-volume-usd",
    "miners_revenue_usd": "miners-revenue",
}

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


def ensure_columns(con):
    existing = {
        row[1]
        for row in con.execute(
            "PRAGMA table_info(onchain_history)"
        )
    }

    columns = {
        "hash_rate": "REAL",
        "tx_count": "REAL",
        "tx_volume_usd": "REAL",
        "miners_revenue_usd": "REAL",
    }

    for name, sql_type in columns.items():
        if name not in existing:
            con.execute(
                f"""
                ALTER TABLE onchain_history
                ADD COLUMN {name} {sql_type}
                """
            )

    con.commit()


def fetch_chart(chart_name):
    response = SESSION.get(
        f"{BASE_URL}/{chart_name}",
        params={
            "timespan": "180days",
            "format": "json",
            "sampled": "false",
        },
        timeout=60,
    )

    response.raise_for_status()

    payload = response.json()

    return payload.get("values", [])


def load_all_metrics():
    combined = {}

    for field, chart_name in CHARTS.items():
        print(
            "Downloading:",
            chart_name,
            flush=True,
        )

        rows = fetch_chart(chart_name)

        print(
            "Points:",
            len(rows),
            flush=True,
        )

        for row in rows:
            try:
                ts = int(row["x"])
                value = float(row["y"])

            except (
                KeyError,
                TypeError,
                ValueError,
            ):
                continue

            if ts not in combined:
                combined[ts] = {}

            combined[ts][field] = value

    return combined


def backfill():
    print()
    print(
        "ORACLE X - BLOCKCHAIN.COM ONCHAIN BACKFILL"
    )
    print(
        "Source:",
        SOURCE,
    )
    print(
        "Symbol:",
        SYMBOL,
    )
    print()

    metrics = load_all_metrics()

    con = sqlite3.connect(DB_PATH)

    ensure_columns(con)

    saved = 0
    updated = 0

    for ts_unix in sorted(metrics):
        values = metrics[ts_unix]

        dt = datetime.fromtimestamp(
            ts_unix,
            tz=timezone.utc,
        )

        raw = {
            "source": SOURCE,
            **values,
        }

        existing = con.execute(
            """
            SELECT id
            FROM onchain_history
            WHERE timestamp_unix = ?
              AND symbol = ?
              AND source = ?
            LIMIT 1
            """,
            (
                ts_unix,
                SYMBOL,
                SOURCE,
            ),
        ).fetchone()

        if existing:
            con.execute(
                """
                UPDATE onchain_history
                SET
                    hash_rate = ?,
                    tx_count = ?,
                    tx_volume_usd = ?,
                    miners_revenue_usd = ?,
                    raw_json = ?
                WHERE id = ?
                """,
                (
                    values.get("hash_rate"),
                    values.get("tx_count"),
                    values.get("tx_volume_usd"),
                    values.get("miners_revenue_usd"),
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
                INSERT INTO onchain_history (
                    timestamp,
                    timestamp_unix,
                    symbol,
                    source,
                    hash_rate,
                    tx_count,
                    tx_volume_usd,
                    miners_revenue_usd,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dt.isoformat(),
                    ts_unix,
                    SYMBOL,
                    SOURCE,
                    values.get("hash_rate"),
                    values.get("tx_count"),
                    values.get("tx_volume_usd"),
                    values.get("miners_revenue_usd"),
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
            COUNT(hash_rate),
            COUNT(tx_count),
            COUNT(tx_volume_usd),
            COUNT(miners_revenue_usd)
        FROM onchain_history
        WHERE symbol = ?
          AND source = ?
        """,
        (
            SYMBOL,
            SOURCE,
        ),
    ).fetchone()

    con.close()

    print()
    print(
        "Saved:",
        saved,
    )
    print(
        "Updated:",
        updated,
    )
    print(
        "DB rows:",
        result[0],
    )
    print(
        "First:",
        result[1],
    )
    print(
        "Last:",
        result[2],
    )
    print(
        "Hash rate rows:",
        result[3],
    )
    print(
        "TX count rows:",
        result[4],
    )
    print(
        "TX volume USD rows:",
        result[5],
    )
    print(
        "Miner revenue rows:",
        result[6],
    )
    print()
    print(
        "ONCHAIN BACKFILL COMPLETE"
    )


if __name__ == "__main__":
    backfill()
