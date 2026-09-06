from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

from database.db import connect
from evolution.paper_engine import (
    get_open_paper_trade,
    open_paper_trade,
)


BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
MAX_ENTRY_DELAY_SECONDS = 120


def iso_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(
        ts,
        tz=timezone.utc,
    ).isoformat()


def due_pending_entries() -> list[Dict]:
    now_unix = int(time.time())

    con = connect()

    try:
        rows = con.execute(
            """
            SELECT *
            FROM paper_pending_entries
            WHERE status = 'PENDING'
              AND execute_after_unix <= ?
            ORDER BY execute_after_unix ASC
            """,
            (now_unix,),
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        con.close()


def fetch_exact_binance_open(
    symbol: str,
    candle_open_unix: int,
) -> Optional[float]:
    pair = (
        "BTCUSDT"
        if symbol == "BTC"
        else f"{symbol}USDT"
    )

    response = requests.get(
        BINANCE_KLINES_URL,
        params={
            "symbol": pair,
            "interval": "15m",
            "startTime": int(candle_open_unix) * 1000,
            "limit": 1,
        },
        timeout=10,
    )

    response.raise_for_status()

    rows = response.json()

    if not rows:
        return None

    row = rows[0]

    returned_open_unix = int(row[0] // 1000)

    if returned_open_unix != int(candle_open_unix):
        return None

    return float(row[1])


def mark_executed(
    pending_id: int,
    paper_trade_id: int,
    executed_unix: int,
) -> None:
    con = connect()

    try:
        con.execute(
            """
            UPDATE paper_pending_entries
            SET status = 'EXECUTED',
                executed_timestamp = ?,
                executed_timestamp_unix = ?,
                paper_trade_id = ?
            WHERE id = ?
              AND status = 'PENDING'
            """,
            (
                iso_from_unix(executed_unix),
                executed_unix,
                paper_trade_id,
                pending_id,
            ),
        )

        con.commit()

    finally:
        con.close()


def cancel_pending(
    pending_id: int,
    reason: str,
) -> None:
    con = connect()

    try:
        con.execute(
            """
            UPDATE paper_pending_entries
            SET status = 'CANCELLED',
                cancel_reason = ?
            WHERE id = ?
              AND status = 'PENDING'
            """,
            (
                reason,
                pending_id,
            ),
        )

        con.commit()

    finally:
        con.close()


def execute_pending(
    pending: Dict,
) -> Dict:
    pending_id = int(pending["id"])
    symbol = str(pending["symbol"])
    model_version = str(
        pending["model_version"]
    )
    side = str(pending["side"]).upper()

    signal_atr = pending["signal_atr"]

    if signal_atr is None:
        cancel_pending(
            pending_id,
            "signal_atr missing",
        )

        return {
            "action": "CANCELLED",
            "pending_id": pending_id,
            "reason": "signal_atr missing",
        }

    signal_atr = float(signal_atr)
    stop_atr = float(pending["stop_atr"])
    tp_r = float(pending["tp_r"])

    if signal_atr <= 0:
        cancel_pending(
            pending_id,
            "invalid signal_atr",
        )

        return {
            "action": "CANCELLED",
            "pending_id": pending_id,
            "reason": "invalid signal_atr",
        }

    existing_trade = get_open_paper_trade(
        symbol,
        model_version,
    )

    if existing_trade is not None:
        cancel_pending(
            pending_id,
            (
                "open paper trade already exists: "
                f"{existing_trade['id']}"
            ),
        )

        return {
            "action": "CANCELLED",
            "pending_id": pending_id,
            "reason": "open paper trade exists",
        }

    entry_unix = int(
        pending["execute_after_unix"]
    )

    now_unix = int(time.time())

    if now_unix > entry_unix + MAX_ENTRY_DELAY_SECONDS:
        cancel_pending(
            pending_id,
            "MISSED_ENTRY_WINDOW",
        )

        return {
            "action": "CANCELLED",
            "pending_id": pending_id,
            "reason": "MISSED_ENTRY_WINDOW",
            "entry_unix": entry_unix,
            "now_unix": now_unix,
            "delay_seconds": now_unix - entry_unix,
        }

    entry_price = fetch_exact_binance_open(
        symbol,
        entry_unix,
    )

    if entry_price is None:
        return {
            "action": "WAIT",
            "pending_id": pending_id,
            "reason": "exact Binance 15m open unavailable",
        }

    stop_distance = (
        signal_atr
        * stop_atr
    )

    if side == "LONG":
        stop_loss = (
            entry_price
            - stop_distance
        )
        tp1 = (
            entry_price
            + stop_distance * tp_r
        )

    elif side == "SHORT":
        stop_loss = (
            entry_price
            + stop_distance
        )
        tp1 = (
            entry_price
            - stop_distance * tp_r
        )

    else:
        cancel_pending(
            pending_id,
            f"invalid side: {side}",
        )

        return {
            "action": "CANCELLED",
            "pending_id": pending_id,
            "reason": f"invalid side: {side}",
        }

    trade_id = open_paper_trade(
        symbol=symbol,
        model_version=model_version,
        side=side,
        entry_price=entry_price,
        stop_loss=stop_loss,
        tp1=tp1,
        risk_percent=float(
            pending["risk_percent"]
        ),
        regime=pending["regime"],
        entry_reason=(
            f"{pending['entry_reason']}; "
            f"execution=next_15m_open; "
            f"signal_atr={signal_atr}; "
            f"entry_candle={iso_from_unix(entry_unix)}"
        ),
        signal_id=pending["signal_id"],
        entry_candle_unix=entry_unix,
    )

    mark_executed(
        pending_id,
        trade_id,
        entry_unix,
    )

    return {
        "action": "EXECUTED",
        "pending_id": pending_id,
        "paper_trade_id": trade_id,
        "symbol": symbol,
        "model_version": model_version,
        "side": side,
        "entry_timestamp": iso_from_unix(
            entry_unix
        ),
        "entry_price": round(
            entry_price,
            2,
        ),
        "signal_atr": signal_atr,
        "stop_atr": stop_atr,
        "stop_loss": round(
            stop_loss,
            2,
        ),
        "tp_r": tp_r,
        "tp1": round(
            tp1,
            2,
        ),
    }


def run_once() -> list[Dict]:
    pending = due_pending_entries()

    results = []

    for item in pending:
        try:
            results.append(
                execute_pending(item)
            )
        except Exception as exc:
            results.append({
                "action": "ERROR",
                "pending_id": item["id"],
                "error": str(exc),
            })

    return results


if __name__ == "__main__":
    print()
    print(
        "ORACLE X — PAPER EXECUTION WORKER"
    )
    print("=" * 70)

    results = run_once()

    if not results:
        print("No due pending entries")

    for result in results:
        print()
        for key, value in result.items():
            print(f"{key}: {value}")
