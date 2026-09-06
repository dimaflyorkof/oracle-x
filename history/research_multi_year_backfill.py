from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Dict, List, Tuple

import requests

from config.settings import DB_PATH


SYMBOL = "BTC"
PAIR = "BTCUSDT"
DAYS = int(os.environ.get("ORACLE_RESEARCH_DAYS", "1095"))
LIMIT = 1000
TIMEOUT = 20
PAUSE_SECONDS = 0.15

SPOT_URL = "https://api.binance.com/api/v3/klines"
FUTURES_URL = "https://fapi.binance.com/fapi/v1/klines"

INTERVALS = {
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "ORACLE-X/3.0",
    "Accept": "application/json",
})


def iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()


def fetch_page(url: str, interval: str, start_ms: int, end_ms: int) -> List:
    last_error = None
    for attempt in range(1, 6):
        try:
            response = SESSION.get(
                url,
                params={
                    "symbol": PAIR,
                    "interval": interval,
                    "startTime": int(start_ms),
                    "endTime": int(end_ms),
                    "limit": LIMIT,
                },
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise RuntimeError(f"Unexpected Binance response: {payload!r}")
            return payload
        except (requests.RequestException, RuntimeError) as exc:
            last_error = exc
            if attempt == 5:
                break
            time.sleep(attempt * 2)
    raise RuntimeError(f"Binance request failed after retries: {last_error}")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH), timeout=60)
    con.execute("PRAGMA busy_timeout = 60000")
    con.execute("PRAGMA journal_mode = WAL")
    return con


def parsed(row: List) -> Dict:
    quote_volume = float(row[7])
    buy_volume = max(0.0, float(row[10]))
    sell_volume = max(0.0, quote_volume - buy_volume)
    delta = buy_volume - sell_volume
    total = buy_volume + sell_volume
    return {
        "timestamp_unix": int(row[0]) // 1000,
        "timestamp": iso_from_ms(int(row[0])),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5]),
        "close_time_ms": int(row[6]),
        "quote_volume": quote_volume,
        "trades": int(row[8]),
        "taker_buy_base": float(row[9]),
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "delta": delta,
        "imbalance": delta / total if total > 0 else 0.0,
    }


def market_raw(item: Dict, interval: str) -> str:
    return json.dumps({
        "kind": "research_historical_kline",
        "availability": "candle_close",
        "interval": interval,
        "close_time_ms": item["close_time_ms"],
        "quote_volume": item["quote_volume"],
        "trades": item["trades"],
        "taker_buy_base": item["taker_buy_base"],
        "taker_buy_quote": item["buy_volume"],
    }, ensure_ascii=False)


def flow_raw(item: Dict) -> str:
    return json.dumps({
        "kind": "historical_kline_orderflow_proxy",
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
    }, ensure_ascii=False)


def upsert_market(con: sqlite3.Connection, interval: str, item: Dict) -> str:
    row = con.execute(
        """
        SELECT id FROM market_snapshots
        WHERE symbol = ? AND timeframe = ? AND timestamp_unix = ?
        LIMIT 1
        """,
        (SYMBOL, interval, item["timestamp_unix"]),
    ).fetchone()
    values = (
        item["timestamp"], "Binance", item["open"], item["high"],
        item["low"], item["close"], item["volume"], market_raw(item, interval),
    )
    if row:
        con.execute(
            """
            UPDATE market_snapshots
            SET timestamp=?, source=?, open=?, high=?, low=?, close=?, volume=?, raw_json=?
            WHERE id=?
            """,
            values + (int(row[0]),),
        )
        return "updated"
    con.execute(
        """
        INSERT INTO market_snapshots (
            timestamp,timestamp_unix,symbol,source,timeframe,
            open,high,low,close,volume,raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            item["timestamp"], item["timestamp_unix"], SYMBOL, "Binance", interval,
            item["open"], item["high"], item["low"], item["close"],
            item["volume"], market_raw(item, interval),
        ),
    )
    return "inserted"


def upsert_flow(con: sqlite3.Connection, source: str, item: Dict) -> str:
    row = con.execute(
        """
        SELECT id FROM orderflow_history
        WHERE symbol = ? AND source = ? AND timestamp_unix = ?
        ORDER BY id LIMIT 1
        """,
        (SYMBOL, source, item["timestamp_unix"]),
    ).fetchone()
    values = (
        item["timestamp"], item["imbalance"], item["buy_volume"],
        item["sell_volume"], item["delta"], flow_raw(item),
    )
    if row:
        con.execute(
            """
            UPDATE orderflow_history
            SET timestamp=?, imbalance=?, buy_volume=?, sell_volume=?, delta=?, raw_json=?
            WHERE id=?
            """,
            values + (int(row[0]),),
        )
        return "updated"
    con.execute(
        """
        INSERT INTO orderflow_history (
            timestamp,timestamp_unix,symbol,source,imbalance,
            buy_volume,sell_volume,delta,cvd,raw_json
        ) VALUES (?,?,?,?,?,?,?,?,NULL,?)
        """,
        (
            item["timestamp"], item["timestamp_unix"], SYMBOL, source,
            item["imbalance"], item["buy_volume"], item["sell_volume"],
            item["delta"], flow_raw(item),
        ),
    )
    return "inserted"


def backfill(url: str, interval: str, flow_source: str | None) -> Dict:
    step_ms = INTERVALS[interval]
    now_ms = int(time.time() * 1000)
    latest_open_ms = now_ms - now_ms % step_ms - step_ms
    start_ms = latest_open_ms - (DAYS * 86400 * 1000)
    cursor_ms = start_ms - start_ms % step_ms
    inserted = 0
    updated = 0
    accepted = 0
    con = connect()
    try:
        if flow_source is not None:
            earliest = con.execute(
                """
                SELECT MIN(timestamp_unix) FROM orderflow_history
                WHERE symbol = ? AND source = ?
                """,
                (SYMBOL, flow_source),
            ).fetchone()[0]
        else:
            earliest = con.execute(
                """
                SELECT MIN(timestamp_unix) FROM market_snapshots
                WHERE symbol = ? AND timeframe = ?
                """,
                (SYMBOL, interval),
            ).fetchone()[0]
        target_end_ms = latest_open_ms
        if earliest is not None:
            target_end_ms = min(
                target_end_ms,
                (int(earliest) * 1000) - step_ms,
            )

        while cursor_ms <= target_end_ms:
            rows = fetch_page(url, interval, cursor_ms, target_end_ms)
            if not rows:
                break
            for raw_row in rows:
                item = parsed(raw_row)
                if item["close_time_ms"] > now_ms:
                    continue
                if url == SPOT_URL:
                    action = upsert_market(con, interval, item)
                    inserted += action == "inserted"
                    updated += action == "updated"
                if flow_source is not None:
                    action = upsert_flow(con, flow_source, item)
                    inserted += action == "inserted"
                    updated += action == "updated"
                accepted += 1
            con.commit()
            last_open_ms = int(rows[-1][0])
            next_cursor = last_open_ms + step_ms
            if next_cursor <= cursor_ms:
                raise RuntimeError("Pagination did not advance")
            cursor_ms = next_cursor
            if accepted % 10000 < len(rows):
                print(
                    f"{flow_source or 'spot_market'} {interval}: "
                    f"rows={accepted} inserted={inserted} updated={updated}",
                    flush=True,
                )
            if len(rows) < LIMIT:
                break
            time.sleep(PAUSE_SECONDS)
    finally:
        con.close()
    return {
        "url": url,
        "interval": interval,
        "flow_source": flow_source,
        "rows": accepted,
        "inserted": inserted,
        "updated": updated,
        "start": iso_from_ms(cursor_ms if accepted == 0 else start_ms),
        "end": iso_from_ms(target_end_ms + step_ms - 1),
    }


def main() -> None:
    if DAYS < 180 or DAYS > 3650:
        raise ValueError("ORACLE_RESEARCH_DAYS must be between 180 and 3650")
    print("ORACLE X — MULTI-YEAR RESEARCH BACKFILL", flush=True)
    print(f"Days: {DAYS}", flush=True)
    jobs: List[Tuple[str, str, str | None]] = [
        (SPOT_URL, "15m", "binance_spot_kline_15m"),
        (FUTURES_URL, "15m", "binance_futures_kline_15m"),
        (SPOT_URL, "1h", None),
        (SPOT_URL, "4h", None),
    ]
    results = []
    for url, interval, source in jobs:
        print(f"START {source or 'market'} {interval}", flush=True)
        results.append(backfill(url, interval, source))
    print("BACKFILL COMPLETE", flush=True)
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
