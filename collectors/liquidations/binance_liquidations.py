import json
import sqlite3
import time
from datetime import datetime, timezone

import websocket

from config.settings import DB_PATH


SYMBOL = "BTC"
SOURCE = "binance_futures"

WS_URL = (
    "wss://fstream.binance.com/ws/"
    "btcusdt@forceOrder"
)


def save_liquidation(data):
    order = data.get("o", {})

    side = order.get("S")

    qty = float(order.get("q", 0) or 0)

    avg_price = float(
        order.get("ap", 0)
        or order.get("p", 0)
        or 0
    )

    if qty <= 0 or avg_price <= 0:
        return

    notional = qty * avg_price

    event_ms = int(
        data.get("E")
        or order.get("T")
        or time.time() * 1000
    )

    ts_unix = event_ms // 1000

    timestamp = datetime.fromtimestamp(
        event_ms / 1000,
        tz=timezone.utc,
    ).isoformat()

    long_liquidations = 0.0
    short_liquidations = 0.0

    # SELL force order = long position liquidated
    if side == "SELL":
        long_liquidations = notional
        dominant_side = "LONG"

    # BUY force order = short position liquidated
    elif side == "BUY":
        short_liquidations = notional
        dominant_side = "SHORT"

    else:
        dominant_side = "UNKNOWN"

    con = sqlite3.connect(DB_PATH)

    con.execute(
        """
        INSERT INTO liquidation_history (
            timestamp,
            timestamp_unix,
            symbol,
            source,
            long_liquidations,
            short_liquidations,
            total_liquidations,
            dominant_side,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            timestamp,
            ts_unix,
            SYMBOL,
            SOURCE,
            long_liquidations,
            short_liquidations,
            notional,
            dominant_side,
            json.dumps(
                data,
                ensure_ascii=False,
            ),
        ),
    )

    con.commit()
    con.close()

    print(
        f"{timestamp} | "
        f"{dominant_side} liquidation | "
        f"${notional:,.2f}"
    )


def on_message(ws, message):
    try:
        data = json.loads(message)

        if data.get("e") != "forceOrder":
            return

        save_liquidation(data)

    except Exception as exc:
        print("MESSAGE ERROR:", exc)


def on_error(ws, error):
    print("WS ERROR:", error)


def on_close(
    ws,
    close_status_code,
    close_msg,
):
    print(
        "WS CLOSED:",
        close_status_code,
        close_msg,
    )


def on_open(ws):
    print()
    print("ORACLE X - BINANCE LIQUIDATIONS")
    print("Symbol:", SYMBOL)
    print("Source:", SOURCE)
    print("Status: LIVE")
    print()
    print("Waiting for liquidations...")
    print()


def run():
    while True:
        try:
            ws = websocket.WebSocketApp(
                WS_URL,
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )

            ws.run_forever(
                ping_interval=20,
                ping_timeout=10,
            )

        except KeyboardInterrupt:
            print()
            print("Collector stopped")
            break

        except Exception as exc:
            print("COLLECTOR ERROR:", exc)

        print("Reconnect in 5 seconds...")
        time.sleep(5)


if __name__ == "__main__":
    run()
