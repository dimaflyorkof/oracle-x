import csv
import gzip
import io
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests

from config.settings import DB_PATH


BASE_URL = "https://public.bybit.com/trading/BTCUSDT"
SYMBOL_DB = "BTC"
SOURCE = "bybit_futures"

TIMEOUT = 60

session = requests.Session()
session.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "*/*",
})


def process_day(day):
    date_str = day.strftime("%Y-%m-%d")

    url = (
        f"{BASE_URL}/"
        f"BTCUSDT{date_str}.csv.gz"
    )

    hourly = defaultdict(
        lambda: {
            "buy": 0.0,
            "sell": 0.0,
        }
    )

    with session.get(
        url,
        stream=True,
        timeout=TIMEOUT,
    ) as response:

        if response.status_code == 404:
            print(
                f"{date_str} | archive not found"
            )
            return {}

        response.raise_for_status()

        response.raw.decode_content = True

        with gzip.GzipFile(
            fileobj=response.raw
        ) as gz:

            with io.TextIOWrapper(
                gz,
                encoding="utf-8",
                errors="replace",
                newline="",
            ) as text_stream:

                reader = csv.DictReader(
                    text_stream
                )

                for row in reader:
                    try:
                        ts = float(
                            row["timestamp"]
                        )

                        side = row["side"]
                        size = float(
                            row["size"]
                        )

                    except (
                        ValueError,
                        TypeError,
                        KeyError,
                    ):
                        continue

                    hour_ts = int(
                        ts // 3600 * 3600
                    )

                    if side == "Buy":
                        hourly[hour_ts]["buy"] += size

                    elif side == "Sell":
                        hourly[hour_ts]["sell"] += size

    return hourly


def save_hour(
    con,
    timestamp_unix,
    buy_volume,
    sell_volume,
):
    timestamp = datetime.fromtimestamp(
        timestamp_unix,
        tz=timezone.utc,
    ).isoformat()

    if sell_volume == 0:
        taker_ratio = None
    else:
        taker_ratio = (
            buy_volume / sell_volume
        )

    con.execute(
        """
        INSERT INTO derivatives_history (
            timestamp,
            timestamp_unix,
            symbol,
            source,
            taker_buy_volume,
            taker_sell_volume,
            taker_ratio
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)

        ON CONFLICT(
            symbol,
            source,
            timestamp_unix
        )
        DO UPDATE SET
            taker_buy_volume =
                excluded.taker_buy_volume,
            taker_sell_volume =
                excluded.taker_sell_volume,
            taker_ratio =
                excluded.taker_ratio,
            timestamp =
                excluded.timestamp
        """,
        (
            timestamp,
            timestamp_unix,
            SYMBOL_DB,
            SOURCE,
            buy_volume,
            sell_volume,
            taker_ratio,
        ),
    )


def backfill_taker(days=180):
    now = datetime.now(
        timezone.utc
    )

    start_day = (
        now - timedelta(days=days)
    ).date()

    end_day = (
        now - timedelta(days=1)
    ).date()

    print()
    print(
        "ORACLE X - BYBIT TAKER BACKFILL"
    )
    print(f"Symbol: {SYMBOL_DB}")
    print(f"Source: {SOURCE}")
    print(f"History: {days} days")
    print()

    con = sqlite3.connect(
        DB_PATH
    )

    total_hours = 0
    current_day = start_day

    while current_day <= end_day:
        day_dt = datetime.combine(
            current_day,
            datetime.min.time(),
            tzinfo=timezone.utc,
        )

        hourly = process_day(
            day_dt
        )

        if hourly:
            for timestamp_unix in sorted(
                hourly
            ):
                buy_volume = (
                    hourly[
                        timestamp_unix
                    ]["buy"]
                )

                sell_volume = (
                    hourly[
                        timestamp_unix
                    ]["sell"]
                )

                save_hour(
                    con,
                    timestamp_unix,
                    buy_volume,
                    sell_volume,
                )

            con.commit()

            total_hours += len(
                hourly
            )

            print(
                f"{current_day} | "
                f"hours={len(hourly)}"
            )

        current_day += timedelta(
            days=1
        )

        time.sleep(0.10)

    count = con.execute(
        """
        SELECT COUNT(*)
        FROM derivatives_history
        WHERE symbol = ?
          AND source = ?
          AND taker_buy_volume IS NOT NULL
          AND taker_sell_volume IS NOT NULL
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
          AND taker_buy_volume IS NOT NULL
          AND taker_sell_volume IS NOT NULL
        """,
        (
            SYMBOL_DB,
            SOURCE,
        ),
    ).fetchone()

    con.close()

    print()
    print(
        f"Processed hourly rows: "
        f"{total_hours}"
    )
    print(
        f"Taker rows in DB: "
        f"{count}"
    )
    print(
        f"First: "
        f"{first_last[0]}"
    )
    print(
        f"Last:  "
        f"{first_last[1]}"
    )
    print()
    print(
        "BYBIT TAKER BACKFILL COMPLETE"
    )


if __name__ == "__main__":
    backfill_taker(
        days=180
    )
