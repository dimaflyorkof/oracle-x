import json
import sqlite3
import time
from datetime import datetime, timezone

import websocket

from config.settings import DB_PATH


SYMBOL = "BTC"
SOURCE = "binance_futures"

WS_URL = (
    "wss://fstream.binance.com/market/ws/"
    "btcusdt@aggTrade"
)


current_minute = None
buy_volume = 0.0
sell_volume = 0.0
cvd = 0.0


def minute_bucket(event_ms):
    ts_sec = event_ms // 1000
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
        ORDER BY timestamp_unix DESC, id DESC
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


def save_minute(timestamp_unix, buy_vol, sell_vol):
    global cvd

    total = buy_vol + sell_vol

    delta = buy_vol - sell_vol

    imbalance = (
        delta / total
        if total > 0
        else 0.0
    )

    cvd += delta

    timestamp = datetime.fromtimestamp(
        timestamp_unix,
        tz=timezone.utc,
    ).isoformat()

    raw = {
        "buy_volume_btc": buy_vol,
        "sell_volume_btc": sell_vol,
        "delta_btc": delta,
        "cvd_btc": cvd,
        "imbalance": imbalance,
    }

    con = sqlite3.connect(DB_PATH)

    existing = con.execute(
        """
        SELECT 1
        FROM orderflow_history
        WHERE timestamp_unix = ?
          AND symbol = ?
          AND source = ?
        LIMIT 1
        """,
        (
            timestamp_unix,
            SYMBOL,
            SOURCE,
        ),
    ).fetchone()

    if existing:
        con.close()
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
        f"BUY={buy_vol:,.4f} BTC | "
        f"SELL={sell_vol:,.4f} BTC | "
        f"DELTA={delta:,.4f} BTC | "
        f"CVD={cvd:,.4f} BTC | "
        f"IMB={imbalance:.4f}",
        flush=True,
    )


def process_trade(data):
    global current_minute
    global buy_volume
    global sell_volume

    price = float(data.get("p", 0) or 0)
    qty = float(data.get("q", 0) or 0)

    if price <= 0 or qty <= 0:
        return

    event_ms = int(
        data.get("T")
        or data.get("E")
        or time.time() * 1000
    )

    minute = minute_bucket(event_ms)

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

    notional = qty

    buyer_is_maker = bool(data.get("m"))

    if buyer_is_maker:
        sell_volume += notional
    else:
        buy_volume += notional


def on_message(ws, message):
    try:
        data = json.loads(message)

        if data.get("e") != "aggTrade":
            return

        process_trade(data)

    except Exception as exc:
        print(
            "MESSAGE ERROR:",
            exc,
            flush=True,
        )


def on_error(ws, error):
    print(
        "WS ERROR:",
        error,
        flush=True,
    )


def on_close(
    ws,
    close_status_code,
    close_msg,
):
    print(
        "WS CLOSED:",
        close_status_code,
        close_msg,
        flush=True,
    )


def on_open(ws):
    print()
    print(
        "ORACLE X - BINANCE ORDERFLOW",
        flush=True,
    )
    print(
        "Mode: WebSocket aggTrade + 1m aggregation",
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
        "Status: LIVE",
        flush=True,
    )
    print()


def run():
    load_cvd()

    print(
        "Restored CVD:",
        round(cvd, 2),
        flush=True,
    )

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
            print(
                "Collector stopped",
                flush=True,
            )
            break

        except Exception as exc:
            print(
                "COLLECTOR ERROR:",
                exc,
                flush=True,
            )

        print(
            "Reconnect in 5 seconds...",
            flush=True,
        )

        time.sleep(5)


if __name__ == "__main__":
    run()
