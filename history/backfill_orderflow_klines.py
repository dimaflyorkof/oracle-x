from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Tuple

import requests

from database.db import connect


SYMBOL = "BTC"
PAIR = "BTCUSDT"
INTERVAL = "15m"
INTERVAL_MS = 15 * 60 * 1000
DAYS = 180
LIMIT = 1000
TIMEOUT = 20
REQUEST_PAUSE_SECONDS = 0.20

MARKETS = (
    (
        "binance_spot_kline_15m",
        "https://api.binance.com/api/v3/klines",
    ),
    (
        "binance_futures_kline_15m",
        "https://fapi.binance.com/fapi/v1/klines",
    ),
)

SESSION = requests.Session()
SESSION.headers.update(
    {
        "User-Agent": "ORACLE-X/2.0",
        "Accept": "application/json",
    }
)


def floor_interval_ms(value: int) -> int:
    return value - value % INTERVAL_MS


def iso_from_ms(value: int) -> str:
    return datetime.fromtimestamp(
        value / 1000.0,
        tz=timezone.utc,
    ).isoformat()


def fetch_page(
    url: str,
    start_ms: int,
    end_ms: int,
) -> List:
    response = SESSION.get(
        url,
        params={
            "symbol": PAIR,
            "interval": INTERVAL,
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


def parse_row(row: List) -> Dict:
    if len(row) < 11:
        raise ValueError("Incomplete Binance kline row")

    open_time_ms = int(row[0])
    close_time_ms = int(row[6])
    quote_volume = float(row[7])
    trades = int(row[8])
    taker_buy_base = float(row[9])
    taker_buy_quote = float(row[10])

    buy_volume = max(0.0, taker_buy_quote)
    sell_volume = max(0.0, quote_volume - buy_volume)
    delta = buy_volume - sell_volume
    total = buy_volume + sell_volume
    imbalance = delta / total if total > 0 else 0.0

    return {
        "timestamp": iso_from_ms(open_time_ms),
        "timestamp_unix": open_time_ms // 1000,
        "close_time_ms": close_time_ms,
        "buy_volume": buy_volume,
        "sell_volume": sell_volume,
        "delta": delta,
        "imbalance": imbalance,
        "quote_volume": quote_volume,
        "taker_buy_base": taker_buy_base,
        "trades": trades,
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
    }


def load_existing_ids(source: str) -> Dict[int, int]:
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT id, timestamp_unix
            FROM orderflow_history
            WHERE symbol = ?
              AND source = ?
            """,
            (SYMBOL, source),
        ).fetchall()
        return {
            int(row["timestamp_unix"]): int(row["id"])
            for row in rows
        }
    finally:
        con.close()


def save_page(
    source: str,
    parsed_rows: Iterable[Dict],
    existing_ids: Dict[int, int],
    cvd: float,
) -> Tuple[int, int, float]:
    inserted = 0
    updated = 0
    con = connect()

    try:
        for item in parsed_rows:
            cvd += float(item["delta"])
            raw = json.dumps(
                {
                    "kind": "historical_kline_orderflow_proxy",
                    "availability": "candle_close",
                    "interval": INTERVAL,
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
            timestamp_unix = int(item["timestamp_unix"])
            values = (
                item["timestamp"],
                float(item["imbalance"]),
                float(item["buy_volume"]),
                float(item["sell_volume"]),
                float(item["delta"]),
                float(cvd),
                raw,
            )

            existing_id = existing_ids.get(timestamp_unix)
            if existing_id is None:
                cursor = con.execute(
                    """
                    INSERT INTO orderflow_history (
                        timestamp,
                        timestamp_unix,
                        symbol,
                        source,
                        bid_volume,
                        ask_volume,
                        imbalance,
                        buy_volume,
                        sell_volume,
                        delta,
                        cvd,
                        spread,
                        raw_json
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
                existing_ids[timestamp_unix] = int(cursor.lastrowid)
                inserted += 1
            else:
                con.execute(
                    """
                    UPDATE orderflow_history
                    SET timestamp = ?,
                        imbalance = ?,
                        buy_volume = ?,
                        sell_volume = ?,
                        delta = ?,
                        cvd = ?,
                        raw_json = ?
                    WHERE id = ?
                    """,
                    values + (existing_id,),
                )
                updated += 1

        con.commit()
    finally:
        con.close()

    return inserted, updated, cvd


def backfill_market(source: str, url: str) -> Dict:
    now_ms = int(time.time() * 1000)
    latest_closed_open_ms = floor_interval_ms(now_ms) - INTERVAL_MS
    start_ms = latest_closed_open_ms - (DAYS - 1) * 24 * 60 * 60 * 1000
    final_close_ms = latest_closed_open_ms + INTERVAL_MS - 1

    existing_ids = load_existing_ids(source)
    cursor_ms = start_ms
    cvd = 0.0
    inserted_total = 0
    updated_total = 0
    accepted_total = 0

    print()
    print(f"SOURCE: {source}", flush=True)
    print(f"FROM:   {iso_from_ms(start_ms)}", flush=True)
    print(f"TO:     {iso_from_ms(final_close_ms)}", flush=True)

    while cursor_ms <= latest_closed_open_ms:
        rows = fetch_page(url, cursor_ms, latest_closed_open_ms)
        if not rows:
            break

        parsed = []
        for row in rows:
            item = parse_row(row)
            if item["close_time_ms"] > now_ms:
                continue
            if item["timestamp_unix"] * 1000 < start_ms:
                continue
            parsed.append(item)

        inserted, updated, cvd = save_page(
            source,
            parsed,
            existing_ids,
            cvd,
        )
        inserted_total += inserted
        updated_total += updated
        accepted_total += len(parsed)

        last_open_ms = int(rows[-1][0])
        next_cursor = last_open_ms + INTERVAL_MS
        if next_cursor <= cursor_ms:
            raise RuntimeError("Binance pagination did not advance")
        cursor_ms = next_cursor

        print(
            f"{source}: rows={accepted_total} "
            f"inserted={inserted_total} updated={updated_total}",
            flush=True,
        )
        time.sleep(REQUEST_PAUSE_SECONDS)

    return {
        "source": source,
        "rows": accepted_total,
        "inserted": inserted_total,
        "updated": updated_total,
        "start": iso_from_ms(start_ms),
        "end": iso_from_ms(final_close_ms),
    }


def main() -> None:
    print("ORACLE X — ORDERFLOW KLINE BACKFILL", flush=True)
    print("BTCUSDT spot + futures | 15m | 180 days", flush=True)
    print(
        "Proxy: taker buy quote volume vs remaining quote volume",
        flush=True,
    )

    results = []
    for source, url in MARKETS:
        results.append(backfill_market(source, url))

    print()
    print("BACKFILL COMPLETE", flush=True)
    print(json.dumps(results, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
