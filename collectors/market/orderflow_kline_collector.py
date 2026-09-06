from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from database.db import connect
from history.backfill_orderflow_klines import (
    INTERVAL_MS,
    MARKETS,
    SYMBOL,
    fetch_page,
    floor_interval_ms,
    parse_row,
)


POLL_SECONDS = 60
STARTUP_FALLBACK_HOURS = 48


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def latest_source_timestamp(source: str) -> Optional[int]:
    con = connect()
    try:
        row = con.execute(
            """
            SELECT MAX(timestamp_unix) AS latest_ts
            FROM orderflow_history
            WHERE symbol = ? AND source = ?
            """,
            (SYMBOL, source),
        ).fetchone()
        value = row["latest_ts"]
        return int(value) if value is not None else None
    finally:
        con.close()


def previous_cvd(con, source: str, timestamp_unix: int) -> float:
    row = con.execute(
        """
        SELECT cvd
        FROM orderflow_history
        WHERE symbol = ?
          AND source = ?
          AND timestamp_unix < ?
          AND cvd IS NOT NULL
        ORDER BY timestamp_unix DESC, id DESC
        LIMIT 1
        """,
        (SYMBOL, source, int(timestamp_unix)),
    ).fetchone()
    return float(row["cvd"]) if row and row["cvd"] is not None else 0.0


def insert_closed_rows(source: str, items: List[Dict]) -> int:
    inserted = 0
    con = connect()
    try:
        for item in sorted(items, key=lambda value: value["timestamp_unix"]):
            timestamp_unix = int(item["timestamp_unix"])
            exists = con.execute(
                """
                SELECT 1
                FROM orderflow_history
                WHERE symbol = ? AND source = ? AND timestamp_unix = ?
                LIMIT 1
                """,
                (SYMBOL, source, timestamp_unix),
            ).fetchone()
            if exists:
                continue

            cvd = previous_cvd(con, source, timestamp_unix) + float(item["delta"])
            raw = json.dumps(
                {
                    "kind": "live_kline_orderflow_proxy",
                    "availability": "candle_close",
                    "interval": "15m",
                    "close_time_ms": item["close_time_ms"],
                    "quote_volume": item["quote_volume"],
                    "taker_buy_base_volume": item["taker_buy_base"],
                    "taker_buy_quote_volume": item["buy_volume"],
                    "trades": item["trades"],
                    "open": item["open"],
                    "high": item["high"],
                    "low": item["low"],
                    "close": item["close"],
                },
                ensure_ascii=False,
            )
            con.execute(
                """
                INSERT INTO orderflow_history (
                    timestamp, timestamp_unix, symbol, source,
                    bid_volume, ask_volume, imbalance,
                    buy_volume, sell_volume, delta, cvd,
                    spread, raw_json
                )
                VALUES (?, ?, ?, ?, NULL, NULL, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    item["timestamp"],
                    timestamp_unix,
                    SYMBOL,
                    source,
                    float(item["imbalance"]),
                    float(item["buy_volume"]),
                    float(item["sell_volume"]),
                    float(item["delta"]),
                    float(cvd),
                    raw,
                ),
            )
            inserted += 1
        con.commit()
    finally:
        con.close()
    return inserted


def update_source(source: str, url: str, now_ms: int) -> Dict:
    latest_closed_open_ms = floor_interval_ms(now_ms) - INTERVAL_MS
    latest_ts = latest_source_timestamp(source)
    if latest_ts is None:
        cursor_ms = now_ms - STARTUP_FALLBACK_HOURS * 60 * 60 * 1000
        cursor_ms = floor_interval_ms(cursor_ms)
    else:
        cursor_ms = (latest_ts * 1000) + INTERVAL_MS

    inserted_total = 0
    fetched_total = 0
    while cursor_ms <= latest_closed_open_ms:
        rows = fetch_page(url, cursor_ms, latest_closed_open_ms)
        if not rows:
            break
        items = []
        for row in rows:
            item = parse_row(row)
            if int(item["close_time_ms"]) <= now_ms:
                items.append(item)
        fetched_total += len(items)
        inserted_total += insert_closed_rows(source, items)
        next_cursor = int(rows[-1][0]) + INTERVAL_MS
        if next_cursor <= cursor_ms:
            raise RuntimeError("Binance pagination did not advance")
        cursor_ms = next_cursor

    return {
        "source": source,
        "fetched": fetched_total,
        "inserted": inserted_total,
        "latest_closed_open_unix": latest_closed_open_ms // 1000,
    }


def run_once() -> List[Dict]:
    now_ms = int(time.time() * 1000)
    results = []
    for source, url in MARKETS:
        try:
            results.append(update_source(source, url, now_ms))
        except Exception as exc:
            results.append(
                {
                    "source": source,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    print(
        json.dumps(
            {"timestamp": utc_now(), "results": results},
            ensure_ascii=False,
        ),
        flush=True,
    )
    return results


def run_forever() -> None:
    print("ORACLE X — ORDERFLOW KLINE COLLECTOR", flush=True)
    print(f"Poll interval: {POLL_SECONDS}s", flush=True)
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "timestamp": utc_now(),
                        "fatal_cycle_error": f"{type(exc).__name__}: {exc}",
                    }
                ),
                flush=True,
            )
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, POLL_SECONDS - elapsed))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once",
        action="store_true",
        help="Collect missing closed candles once and exit",
    )
    args = parser.parse_args()
    if args.once:
        run_once()
    else:
        try:
            run_forever()
        except KeyboardInterrupt:
            print("Collector stopped", flush=True)


if __name__ == "__main__":
    main()
