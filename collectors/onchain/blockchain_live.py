import json
import sqlite3
import time
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

POLL_SECONDS = 21600  # 6 годин

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
            "timespan": "7days",
            "format": "json",
            "sampled": "false",
        },
        timeout=60,
    )

    response.raise_for_status()

    return response.json().get("values", [])


def fetch_latest_metrics():
    combined = {}

    for field, chart_name in CHARTS.items():
        rows = fetch_chart(chart_name)

        if not rows:
            continue

        latest = rows[-1]

        try:
            ts = int(latest["x"])
            value = float(latest["y"])

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            continue

        combined.setdefault(ts, {})
        combined[ts][field] = value

    return combined


def save_metrics(metrics):
    if not metrics:
        print(
            "No onchain data received",
            flush=True,
        )
        return

    con = sqlite3.connect(DB_PATH)

    ensure_columns(con)

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
                    hash_rate = COALESCE(?, hash_rate),
                    tx_count = COALESCE(?, tx_count),
                    tx_volume_usd = COALESCE(?, tx_volume_usd),
                    miners_revenue_usd = COALESCE(?, miners_revenue_usd),
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

            print(
                "Updated:",
                dt.isoformat(),
                values,
                flush=True,
            )

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

            print(
                "Saved:",
                dt.isoformat(),
                values,
                flush=True,
            )

    con.commit()
    con.close()


def run():
    print()
    print(
        "ORACLE X - BLOCKCHAIN.COM ONCHAIN LIVE",
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
            metrics = fetch_latest_metrics()

            save_metrics(metrics)

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
