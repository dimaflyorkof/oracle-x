import json
import sqlite3
import time
from datetime import datetime, timezone

import requests

from config.settings import DB_PATH


SYMBOL = "BTC"
BINANCE_SYMBOL = "BTCUSDT"
SOURCE = "binance_futures"

BASE_URL = "https://fapi.binance.com"

POLL_SECONDS = 5
LIMIT = 1000

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


current_minute = None
buy_volume = 0.0
sell_volume = 0.0
cvd = 0.0
last_trade_id = None


def minute_bucket(ts_ms):
    ts_sec = ts_ms // 1000
    return (ts_sec // 60) * 60


def fetch_trades():
    params = {
        "symbol": BINANCE_SYMBOL,
        "limit": LIMIT,
    }

    response = SESSION.get(
        BASE_URL + "/fapi/v1/aggTrades",
        params=params,
        timeout=15,
    )

    response.raise_for_status()

    return response.json()


def save_minute(
    timestamp_unix,
    buy_vol,
    sell_vol,
):
    global cvd

    delta = buy_vol - sell_vol
    cvd += delta

    total = buy_vol + sell_vol

    imbalance = (
        delta / total
        if total > 0
        else 0.0
    )

    timestamp = datetime.fromtimestamp(
        timestamp_unix,
        tz=timezone.utc,
    ).isoformat()

    raw = {
        "buy_volume": buy_vol,
        "sell_volume": sell_vol,
        "delta": delta,
        "cvd": cvd,
        "imbalance": imbalance,
    }

    con = sqlite3.connect(DB_PATH)

    con.execute(
        """
        DELETE FROM orderflow_history
        WHERE symbol = ?
          AND source = ?
          AND timestamp_unix = ?
        """,
        (
            SYMBOL,
            SOURCE,
            timestamp_unix,
        ),
    )

    con.execute(
        """
        INSERT INTO orderflow_history (
            timestamp,
            timestamp_unix,
            symbol,
            source,
            buy_volume,
            sell_volume,
            delta,
            cvd,
            imbalance,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp,
            timestamp_unix,
            SYMBOL,
            SOURCE,
            buy_vol,
            sell_vol,
            delta,
            cvd,
            imbalance,
            json.dumps(
                raw,
                ensure_ascii=False,
            ),
        ),
    )

    con.commit()
    con.close()

    print(
        f"{timestamp} | "
        f"BUY={buy_vol:.4f} BTC | "
        f"SELL={sell_vol:.4f} BTC | "
        f"DELTA={delta:.4f} | "
        f"CVD={cvd:.4f}",
        flush=True,
    )


def process_trade(trade):
    global current_minute
    global buy_volume
    global sell_volume
    global last_trade_id

    trade_id = int(trade["a"])

    if (
        last_trade_id is not None
        and trade_id <= last_trade_id
    ):
        return

    ts_ms = int(trade["T"])
    minute = minute_bucket(ts_ms)

    if current_minute is None:
        current_minute = minute

    if minute != current_minute:
        save_minute(
            current_minute,
            buy_volume,
            sell_volume,
        )

        current_minute = minute
        buy_volume = 0.0
        sell_volume = 0.0

    qty = float(trade["q"])

    buyer_is_maker = bool(
        trade["m"]
    )

    if not buyer_is_maker:
        buy_volume += qty
    else:
        sell_volume += qty

    last_trade_id = trade_id


def run():
    print()
    print("ORACLE X - BINANCE ORDER FLOW")
    print("Symbol:", SYMBOL)
    print("Source:", SOURCE)
    print("Mode: REST aggTrades")
    print("Aggregation: 1 minute")
    print("Status: LIVE")
    print()

    while True:
        try:
            trades = fetch_trades()

            for trade in trades:
                process_trade(trade)

        except KeyboardInterrupt:
            print(
                "\nCollector stopped",
                flush=True,
            )
            break

        except Exception as exc:
            print(
                "COLLECTOR ERROR:",
                exc,
                flush=True,
            )

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    run()
