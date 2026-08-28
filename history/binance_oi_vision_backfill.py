import csv
import io
import sqlite3
import zipfile
from datetime import datetime, timedelta, timezone

import requests

from config.settings import DB_PATH


SYMBOL = "BTC"
BINANCE_SYMBOL = "BTCUSDT"
SOURCE = "binance_futures"

BASE_URL = (
    "https://data.binance.vision/data/futures/um/"
    "daily/metrics/BTCUSDT"
)


def download_day(day):
    date_str = day.strftime("%Y-%m-%d")

    url = (
        f"{BASE_URL}/"
        f"{BINANCE_SYMBOL}-metrics-{date_str}.zip"
    )

    r = requests.get(url, timeout=60)

    if r.status_code == 404:
        print(f"{date_str} | NOT FOUND")
        return []

    r.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        name = z.namelist()[0]

        with z.open(name) as f:
            text = io.TextIOWrapper(
                f,
                encoding="utf-8",
                errors="replace",
                newline="",
            )

            reader = csv.DictReader(text)

            return list(reader)


def hour_bucket(dt):
    return dt.replace(
        minute=0,
        second=0,
        microsecond=0,
    )


def aggregate_hourly(rows):
    hourly = {}

    for row in rows:
        try:
            dt = datetime.strptime(
                row["create_time"].strip(),
                "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=timezone.utc)

            oi = float(row["sum_open_interest"])
            long_short = float(
                row["count_long_short_ratio"]
            )
            taker_ratio = float(
                row["sum_taker_long_short_vol_ratio"]
            )

        except (
            KeyError,
            TypeError,
            ValueError,
        ):
            continue

        h = hour_bucket(dt)

        if h not in hourly:
            hourly[h] = {
                "oi": [],
                "long_short": [],
                "taker_ratio": [],
            }

        hourly[h]["oi"].append(oi)
        hourly[h]["long_short"].append(long_short)
        hourly[h]["taker_ratio"].append(taker_ratio)

    result = {}

    for h, values in hourly.items():
        result[h] = {
            "open_interest": (
                sum(values["oi"]) / len(values["oi"])
            ),
            "long_short_ratio": (
                sum(values["long_short"])
                / len(values["long_short"])
            ),
            "taker_ratio": (
                sum(values["taker_ratio"])
                / len(values["taker_ratio"])
            ),
        }

    return result


def save_hour(con, dt, values):
    ts_unix = int(dt.timestamp())

    con.execute(
        """
        INSERT INTO derivatives_history (
            timestamp,
            timestamp_unix,
            symbol,
            source,
            open_interest,
            long_short_ratio,
            taker_ratio
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(symbol, source, timestamp_unix)
        DO UPDATE SET
            open_interest = excluded.open_interest,
            long_short_ratio = excluded.long_short_ratio,
            taker_ratio = excluded.taker_ratio
        """,
        (
            dt.isoformat(),
            ts_unix,
            SYMBOL,
            SOURCE,
            values["open_interest"],
            values["long_short_ratio"],
            values["taker_ratio"],
        ),
    )


def backfill(days=3):
    today = datetime.now(timezone.utc).date()

    start = today - timedelta(days=days)

    con = sqlite3.connect(DB_PATH)

    total_hours = 0

    print()
    print("ORACLE X - BINANCE VISION METRICS")
    print("Symbol:", SYMBOL)
    print("Source:", SOURCE)
    print("History:", days, "days")
    print()

    for i in range(days):
        day = start + timedelta(days=i)

        rows = download_day(day)

        if not rows:
            continue

        hourly = aggregate_hourly(rows)

        for dt, values in sorted(hourly.items()):
            save_hour(
                con,
                dt,
                values,
            )

        con.commit()

        total_hours += len(hourly)

        print(
            f"{day} | raw={len(rows)} "
            f"| hours={len(hourly)}"
        )

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

    con.close()

    print()
    print("Processed hourly rows:", total_hours)
    print("Binance OI rows in DB:", count)
    print()
    print("BINANCE VISION BACKFILL COMPLETE")


if __name__ == "__main__":
    backfill(days=180)
