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


def load_cvd():
    global cvd

    con = sqlite3.connect(DB_PATH)

    row = con.execute(
        """
        SELECT cvd
        FROM orderflow_history
        WHERE symbol = ?
          AND source = ?
        ORDER BY timestamp_unix DESC
        LIMIT 1
        """,
        (
            SYMBOL,
            SOURCE,
        ),
    ).fetchone()

    con.close()

    if row and row[0] is not None:
        cvd = float(row[0])


def fetch_trades(from_id=None):
    params = {
        "symbol": BINANCE_SYMBOL,
        "limit": LIMIT,
    }

    if from_id is not None:
        params["fromId"] = from_id

    response = SESSION.get(
        BASE_URL + "/fapi/v1/aggTrades",
        params=params,
        timeout=15,
    )

    response.raise_for_status()

    return response.json()


def initialize_trade_cursor():
    global last_trade_id

    trades = fetch_trades()

    if not trades:
        return False

    last_trade_id = int(
        trades[-1]["a"]
    )

    print(
        "Initialized trade cursor:",
        last_trade_id,
        flush=True,
    )

    return True


def save_minute(
    timestamp_unix,
    buy_vol,
    sell_vol,
    final_trade_id,
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
        "last_trade_id": final_trade_id,
    }

    con = sqlite3.connect(DB_PATH)

    existing = con.execute(
        """
        SELECT 1
        FROM orderflow_history
        WHERE symbol = ?
          AND source = ?
          AND timestamp_unix = ?
        LIMIT 1
        """,
        (
            SYMBOL,
            SOURCE,
            timestamp_unix,
        ),
    ).fetchone()

    if existing:
        con.close()

        print(
            "SKIP existing minute:",
            timestamp,
            flush=True,
        )

        return

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
        f"CVD={cvd:.4f} | "
        f"trade_id={final_trade_id}",
        flush=True,
    )


def process_trade(trade):
    global current_minute
    global buy_volume
    global sell_volume
    global last_trade_id

    trade_id = int(
        trade["a"]
    )

    if (
        last_trade_id is not None
        and trade_id <= last_trade_id
    ):
        return

    ts_ms = int(
        trade["T"]
    )

    minute = minute_bucket(
        ts_ms
    )

    if current_minute is None:
        current_minute = minute

    if minute != current_minute:
        save_minute(
            current_minute,
            buy_volume,
            sell_volume,
            last_trade_id,
        )

        current_minute = minute
        buy_volume = 0.0
        sell_volume = 0.0

    qty = float(
        trade["q"]
    )

    buyer_is_maker = bool(
        trade["m"]
    )

    if not buyer_is_maker:
        buy_volume += qty
    else:
        sell_volume += qty

    last_trade_id = trade_id


def process_new_trades():
    global last_trade_id

    if last_trade_id is None:
        return

    while True:
        trades = fetch_trades(
            from_id=last_trade_id + 1
        )

        if not trades:
            break

        for trade in trades:
            process_trade(trade)

        if len(trades) < LIMIT:
            break


def run():
    print()
    print(
        "ORACLE X - BINANCE ORDER FLOW",
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
        "Mode: REST aggTrades GAP-SAFE",
        flush=True,
    )
    print(
        "Aggregation: 1 minute",
        flush=True,
    )
    print(
        "Status: LIVE",
        flush=True,
    )
    print()

    load_cvd()

    print(
        "Restored CVD:",
        round(cvd, 4),
        flush=True,
    )

    if not initialize_trade_cursor():
        print(
            "Unable to initialize trade cursor",
            flush=True,
        )

        return

    while True:
        try:
            process_new_trades()

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

        time.sleep(
            POLL_SECONDS
        )


if __name__ == "__main__":
    run()
