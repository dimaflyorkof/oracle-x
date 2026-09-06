from __future__ import annotations

import argparse
import json
import signal
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional

import websocket

from database.db import connect


SYMBOL = "BTC"
INTERVAL_SECONDS = 15 * 60
FINALIZE_GRACE_SECONDS = 10
FLUSH_SECONDS = 15
RECONNECT_MAX_SECONDS = 60

COINBASE_URL = "wss://ws-feed.exchange.coinbase.com"
KRAKEN_URL = "wss://ws.kraken.com/v2"

COINBASE_SOURCE = "coinbase_spot_trades_15m"
KRAKEN_SOURCE = "kraken_spot_trades_15m"


def utc_iso(timestamp_unix: int) -> str:
    return datetime.fromtimestamp(
        int(timestamp_unix),
        tz=timezone.utc,
    ).isoformat()


def parse_rfc3339(value: str) -> float:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).timestamp()


def bucket_open(timestamp: float) -> int:
    return int(timestamp) // INTERVAL_SECONDS * INTERVAL_SECONDS


@dataclass
class FlowBucket:
    source: str
    timestamp_unix: int
    buy_volume: float = 0.0
    sell_volume: float = 0.0
    base_volume: float = 0.0
    trades: int = 0
    first_trade_unix: Optional[float] = None
    last_trade_unix: Optional[float] = None
    first_trade_id: Optional[str] = None
    last_trade_id: Optional[str] = None
    connection_covered_open: bool = False
    connection_interrupted: bool = False
    dirty: bool = False

    def add(
        self,
        *,
        side: str,
        price: float,
        size: float,
        timestamp: float,
        trade_id: str,
    ) -> None:
        notional = price * size
        if side == "BUY":
            self.buy_volume += notional
        elif side == "SELL":
            self.sell_volume += notional
        else:
            raise ValueError(f"Unsupported trade side: {side}")

        self.base_volume += size
        self.trades += 1
        if self.first_trade_unix is None:
            self.first_trade_unix = timestamp
            self.first_trade_id = trade_id
        self.last_trade_unix = timestamp
        self.last_trade_id = trade_id
        self.dirty = True


class RecentTradeIds:
    def __init__(self, limit: int = 100_000) -> None:
        self.limit = limit
        self.queue: deque[str] = deque()
        self.values: set[str] = set()

    def add_if_new(self, value: str) -> bool:
        if value in self.values:
            return False
        self.values.add(value)
        self.queue.append(value)
        while len(self.queue) > self.limit:
            self.values.discard(self.queue.popleft())
        return True


class FlowStore:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.buckets: Dict[tuple[str, int], FlowBucket] = {}
        self.connected_at: Dict[str, float] = {}
        self.last_coinbase_trade_id: Optional[int] = None
        self.seen_ids = {
            COINBASE_SOURCE: RecentTradeIds(),
            KRAKEN_SOURCE: RecentTradeIds(),
        }

    def connected(self, source: str) -> None:
        now = time.time()
        with self.lock:
            self.connected_at[source] = now

    def disconnected(self, source: str) -> None:
        now_bucket = bucket_open(time.time())
        with self.lock:
            self.connected_at.pop(source, None)
            bucket = self.buckets.get((source, now_bucket))
            if bucket is not None:
                bucket.connection_interrupted = True
                bucket.dirty = True

    def add_trade(
        self,
        *,
        source: str,
        side: str,
        price: float,
        size: float,
        timestamp: float,
        trade_id: str,
    ) -> bool:
        if price <= 0 or size <= 0:
            return False

        with self.lock:
            if not self.seen_ids[source].add_if_new(trade_id):
                return False

            opened = bucket_open(timestamp)
            key = (source, opened)
            bucket = self.buckets.get(key)
            if bucket is None:
                connected_at = self.connected_at.get(source)
                bucket = FlowBucket(
                    source=source,
                    timestamp_unix=opened,
                    connection_covered_open=(
                        connected_at is not None
                        and connected_at <= opened + FINALIZE_GRACE_SECONDS
                    ),
                )
                self.buckets[key] = bucket

            if source == COINBASE_SOURCE:
                numeric_trade_id = int(trade_id)
                previous = self.last_coinbase_trade_id
                if previous is not None and numeric_trade_id != previous + 1:
                    bucket.connection_interrupted = True
                if previous is None or numeric_trade_id > previous:
                    self.last_coinbase_trade_id = numeric_trade_id

            bucket.add(
                side=side,
                price=price,
                size=size,
                timestamp=timestamp,
                trade_id=trade_id,
            )
            return True

    def rows_to_flush(self, now: float, force: bool = False) -> list[tuple[FlowBucket, bool]]:
        rows: list[tuple[FlowBucket, bool]] = []
        remove: list[tuple[str, int]] = []
        with self.lock:
            for key, bucket in self.buckets.items():
                finalized = now >= (
                    bucket.timestamp_unix
                    + INTERVAL_SECONDS
                    + FINALIZE_GRACE_SECONDS
                )
                if force or finalized or bucket.dirty:
                    rows.append((FlowBucket(**asdict(bucket)), finalized))
                    bucket.dirty = False
                if finalized:
                    remove.append(key)
            for key in remove:
                del self.buckets[key]
        return rows


STORE = FlowStore()
STOP_EVENT = threading.Event()


def save_bucket(bucket: FlowBucket, finalized: bool) -> None:
    total = bucket.buy_volume + bucket.sell_volume
    imbalance = (
        (bucket.buy_volume - bucket.sell_volume) / total
        if total > 0
        else 0.0
    )
    complete = bool(
        finalized
        and bucket.trades > 0
        and bucket.connection_covered_open
        and not bucket.connection_interrupted
    )
    raw = {
        "kind": "public_trade_aggregate",
        "availability": "candle_close",
        "interval": "15m",
        "complete": complete,
        "quality": "COMPLETE" if complete else "PARTIAL",
        "side_semantics": (
            "coinbase_maker_inverted_to_taker"
            if bucket.source == COINBASE_SOURCE
            else "exchange_reported_taker"
        ),
        "base_volume": bucket.base_volume,
        "quote_volume": total,
        "trades": bucket.trades,
        "first_trade_unix": bucket.first_trade_unix,
        "last_trade_unix": bucket.last_trade_unix,
        "first_trade_id": bucket.first_trade_id,
        "last_trade_id": bucket.last_trade_id,
        "connection_covered_open": bucket.connection_covered_open,
        "connection_interrupted": bucket.connection_interrupted,
        "finalized": finalized,
    }

    con = connect()
    try:
        existing = con.execute(
            """
            SELECT id
            FROM orderflow_history
            WHERE symbol = ? AND source = ? AND timestamp_unix = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (SYMBOL, bucket.source, bucket.timestamp_unix),
        ).fetchone()
        values = (
            utc_iso(bucket.timestamp_unix),
            float(imbalance),
            float(bucket.buy_volume),
            float(bucket.sell_volume),
            float(bucket.buy_volume - bucket.sell_volume),
            json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
        )
        if existing:
            con.execute(
                """
                UPDATE orderflow_history
                SET timestamp = ?, imbalance = ?, buy_volume = ?,
                    sell_volume = ?, delta = ?, raw_json = ?
                WHERE id = ?
                """,
                (*values, int(existing["id"])),
            )
        else:
            con.execute(
                """
                INSERT INTO orderflow_history (
                    timestamp, timestamp_unix, symbol, source,
                    bid_volume, ask_volume, imbalance,
                    buy_volume, sell_volume, delta, cvd, spread, raw_json
                )
                VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    values[0],
                    bucket.timestamp_unix,
                    SYMBOL,
                    bucket.source,
                    *values[1:],
                ),
            )
        con.commit()
    finally:
        con.close()


def flush_loop() -> None:
    while not STOP_EVENT.wait(FLUSH_SECONDS):
        try:
            for bucket, finalized in STORE.rows_to_flush(time.time()):
                save_bucket(bucket, finalized)
        except Exception as exc:
            print(
                json.dumps(
                    {"component": "flush", "error": f"{type(exc).__name__}: {exc}"}
                ),
                flush=True,
            )


def iter_coinbase_trades(message: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    # Coinbase Exchange documents `match.side` as the maker side, so it must
    # be inverted to obtain aggressive/taker flow.  Ignore the initial
    # `last_match` snapshot so it is not counted as a new trade.
    if message.get("type") != "match" or message.get("product_id") != "BTC-USD":
        return
    maker_side = str(message["side"]).upper()
    taker_side = "BUY" if maker_side == "SELL" else "SELL"
    yield {
        "source": COINBASE_SOURCE,
        "trade_id": str(message["trade_id"]),
        "side": taker_side,
        "price": float(message["price"]),
        "size": float(message["size"]),
        "timestamp": parse_rfc3339(str(message["time"])),
    }


def iter_kraken_trades(message: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    if message.get("channel") != "trade" or message.get("type") != "update":
        return
    for trade in message.get("data", []):
        yield {
            "source": KRAKEN_SOURCE,
            "trade_id": str(trade["trade_id"]),
            "side": str(trade["side"]).upper(),
            "price": float(trade["price"]),
            "size": float(trade["qty"]),
            "timestamp": parse_rfc3339(str(trade["timestamp"])),
        }


def run_socket(
    *,
    name: str,
    source: str,
    url: str,
    subscriptions: list[Dict[str, Any]],
    parser,
) -> None:
    delay = 1
    while not STOP_EVENT.is_set():
        def on_open(ws) -> None:
            STORE.connected(source)
            for subscription in subscriptions:
                ws.send(json.dumps(subscription))
            print(json.dumps({"exchange": name, "status": "CONNECTED"}), flush=True)

        def on_message(_ws, raw_message: str) -> None:
            try:
                message = json.loads(raw_message)
                accepted = 0
                for trade in parser(message):
                    accepted += int(STORE.add_trade(**trade))
                if accepted:
                    delay_holder[0] = 1
            except Exception as exc:
                print(
                    json.dumps(
                        {
                            "exchange": name,
                            "status": "MESSAGE_ERROR",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    ),
                    flush=True,
                )

        def on_error(_ws, error) -> None:
            print(
                json.dumps(
                    {"exchange": name, "status": "SOCKET_ERROR", "error": str(error)}
                ),
                flush=True,
            )

        def on_close(_ws, code, reason) -> None:
            STORE.disconnected(source)
            print(
                json.dumps(
                    {
                        "exchange": name,
                        "status": "DISCONNECTED",
                        "code": code,
                        "reason": reason,
                    }
                ),
                flush=True,
            )

        delay_holder = [delay]
        app = websocket.WebSocketApp(
            url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close,
        )
        app.run_forever(ping_interval=20, ping_timeout=10)
        delay = delay_holder[0]
        if STOP_EVENT.wait(delay):
            break
        delay = min(RECONNECT_MAX_SECONDS, delay * 2)


def self_test() -> None:
    coinbase = {
        "type": "match",
        "trade_id": 1,
        "product_id": "BTC-USD",
        "price": "100.0",
        "size": "2.0",
        "side": "sell",
        "time": "2026-01-01T00:01:00Z",
    }
    kraken = {
        "channel": "trade",
        "type": "update",
        "data": [{
            "symbol": "BTC/USD",
            "side": "sell",
            "qty": 3.0,
            "price": 100.0,
            "trade_id": 7,
            "timestamp": "2026-01-01T00:02:00Z",
        }],
    }
    coinbase_rows = list(iter_coinbase_trades(coinbase))
    kraken_rows = list(iter_kraken_trades(kraken))
    assert coinbase_rows[0]["side"] == "BUY"
    assert coinbase_rows[0]["price"] * coinbase_rows[0]["size"] == 200.0
    assert kraken_rows[0]["side"] == "SELL"
    assert kraken_rows[0]["price"] * kraken_rows[0]["size"] == 300.0
    assert bucket_open(1767225660) == 1767225600
    print("GLOBAL SPOT FLOW SELF-TEST: PASSED")


def stop_handler(_signum, _frame) -> None:
    STOP_EVENT.set()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return

    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    print("ORACLE X — GLOBAL SPOT FLOW COLLECTOR", flush=True)
    print("Sources: Coinbase BTC-USD, Kraken BTC/USD", flush=True)
    print("Mode: collection only; no V5.1 trading influence", flush=True)

    workers = [
        threading.Thread(
            target=run_socket,
            kwargs={
                "name": "coinbase",
                "source": COINBASE_SOURCE,
                "url": COINBASE_URL,
                "subscriptions": [{
                    "type": "subscribe",
                    "product_ids": ["BTC-USD"],
                    "channels": ["matches", "heartbeat"],
                }],
                "parser": iter_coinbase_trades,
            },
            daemon=True,
            name="coinbase-flow",
        ),
        threading.Thread(
            target=run_socket,
            kwargs={
                "name": "kraken",
                "source": KRAKEN_SOURCE,
                "url": KRAKEN_URL,
                "subscriptions": [{
                    "method": "subscribe",
                    "params": {
                        "channel": "trade",
                        "symbol": ["BTC/USD"],
                        "snapshot": False,
                    },
                }],
                "parser": iter_kraken_trades,
            },
            daemon=True,
            name="kraken-flow",
        ),
        threading.Thread(target=flush_loop, daemon=True, name="flow-flush"),
    ]
    for worker in workers:
        worker.start()

    while not STOP_EVENT.wait(1):
        if not workers[0].is_alive() or not workers[1].is_alive():
            raise RuntimeError("A websocket worker exited unexpectedly")

    for bucket, finalized in STORE.rows_to_flush(time.time(), force=True):
        save_bucket(bucket, finalized)
    print("Collector stopped cleanly", flush=True)


if __name__ == "__main__":
    main()
