import json
import sqlite3
import time
from datetime import datetime, timezone

import requests

from config.settings import DB_PATH


BASE_URL = "https://fapi.binance.com"

BINANCE_SYMBOL = "BTCUSDT"
SYMBOL = "BTC"
SOURCE = "binance_futures"

TIMEOUT = 15
POLL_SECONDS = 300


SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


def get_json(path, params=None):
    response = SESSION.get(
        BASE_URL + path,
        params=params,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    return response.json()


def fetch_funding():
    data = get_json(
        "/fapi/v1/fundingRate",
        {
            "symbol": BINANCE_SYMBOL,
            "limit": 1,
        },
    )

    if not data:
        return {}

    row = data[-1]

    return {
        "funding_rate": float(
            row["fundingRate"]
        ),
        "funding_timestamp_unix": int(
            row["fundingTime"]
        ) // 1000,
    }


def fetch_open_interest():
    data = get_json(
        "/fapi/v1/openInterest",
        {
            "symbol": BINANCE_SYMBOL,
        },
    )

    return float(
        data["openInterest"]
    )


def fetch_long_short():
    data = get_json(
        "/futures/data/globalLongShortAccountRatio",
        {
            "symbol": BINANCE_SYMBOL,
            "period": "5m",
            "limit": 1,
        },
    )

    if not data:
        return {}

    row = data[-1]

    return {
        "long_ratio": float(
            row["longAccount"]
        ),
        "short_ratio": float(
            row["shortAccount"]
        ),
        "long_short_ratio": float(
            row["longShortRatio"]
        ),
    }


def fetch_taker():
    data = get_json(
        "/futures/data/takerlongshortRatio",
        {
            "symbol": BINANCE_SYMBOL,
            "period": "5m",
            "limit": 1,
        },
    )

    if not data:
        return {}

    row = data[-1]

    return {
        "taker_buy_volume": float(
            row["buyVol"]
        ),
        "taker_sell_volume": float(
            row["sellVol"]
        ),
        "taker_ratio": float(
            row["buySellRatio"]
        ),
    }


def fetch_snapshot():
    now = datetime.now(
        timezone.utc
    )

    result = {
        "timestamp": now.isoformat(),
        "timestamp_unix": int(
            now.timestamp()
        ),
        "symbol": SYMBOL,
        "source": SOURCE,
    }

    result.update(
        fetch_funding()
    )

    result[
        "open_interest"
    ] = fetch_open_interest()

    result.update(
        fetch_long_short()
    )

    result.update(
        fetch_taker()
    )

    return result


def save_snapshot(snapshot):
    con = sqlite3.connect(
        DB_PATH
    )

    ts_unix = snapshot[
        "timestamp_unix"
    ]

    # 5-хвилинний bucket
    bucket = (
        ts_unix // 300
    ) * 300

    dt = datetime.fromtimestamp(
        bucket,
        tz=timezone.utc,
    )

    existing = con.execute(
        """
        SELECT id
        FROM derivatives_history
        WHERE symbol = ?
          AND source = ?
          AND timestamp_unix = ?
        LIMIT 1
        """,
        (
            SYMBOL,
            SOURCE,
            bucket,
        ),
    ).fetchone()

    raw = dict(snapshot)

    if existing:
        con.execute(
            """
            UPDATE derivatives_history
            SET
                timestamp = ?,
                funding_rate = ?,
                open_interest = ?,
                long_ratio = ?,
                short_ratio = ?,
                long_short_ratio = ?,
                taker_buy_volume = ?,
                taker_sell_volume = ?,
                taker_ratio = ?,
                raw_json = ?
            WHERE id = ?
            """,
            (
                dt.isoformat(),
                snapshot.get(
                    "funding_rate"
                ),
                snapshot.get(
                    "open_interest"
                ),
                snapshot.get(
                    "long_ratio"
                ),
                snapshot.get(
                    "short_ratio"
                ),
                snapshot.get(
                    "long_short_ratio"
                ),
                snapshot.get(
                    "taker_buy_volume"
                ),
                snapshot.get(
                    "taker_sell_volume"
                ),
                snapshot.get(
                    "taker_ratio"
                ),
                json.dumps(
                    raw,
                    ensure_ascii=False,
                ),
                existing[0],
            ),
        )

        action = "Updated"

    else:
        con.execute(
            """
            INSERT INTO derivatives_history (
                timestamp,
                timestamp_unix,
                symbol,
                source,
                funding_rate,
                open_interest,
                long_ratio,
                short_ratio,
                long_short_ratio,
                taker_buy_volume,
                taker_sell_volume,
                taker_ratio,
                raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dt.isoformat(),
                bucket,
                SYMBOL,
                SOURCE,
                snapshot.get(
                    "funding_rate"
                ),
                snapshot.get(
                    "open_interest"
                ),
                snapshot.get(
                    "long_ratio"
                ),
                snapshot.get(
                    "short_ratio"
                ),
                snapshot.get(
                    "long_short_ratio"
                ),
                snapshot.get(
                    "taker_buy_volume"
                ),
                snapshot.get(
                    "taker_sell_volume"
                ),
                snapshot.get(
                    "taker_ratio"
                ),
                json.dumps(
                    raw,
                    ensure_ascii=False,
                ),
            ),
        )

        action = "Saved"

    con.commit()
    con.close()

    print(
        f"{action}:",
        dt.isoformat(),
        "| OI=",
        snapshot.get(
            "open_interest"
        ),
        "| funding=",
        snapshot.get(
            "funding_rate"
        ),
        "| L/S=",
        snapshot.get(
            "long_short_ratio"
        ),
        "| taker=",
        snapshot.get(
            "taker_ratio"
        ),
        flush=True,
    )


def run():
    print()
    print(
        "ORACLE X - BINANCE FUTURES LIVE",
        flush=True,
    )
    print(
        "Symbol:",
        SYMBOL,
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
            snapshot = (
                fetch_snapshot()
            )

            save_snapshot(
                snapshot
            )

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
