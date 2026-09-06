from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List

from database.db import connect
from evolution.paper_engine import list_open_paper_trades
from evolution.paper_pending import get_model_config


def iso_from_unix(ts: int) -> str:
    return datetime.fromtimestamp(
        int(ts),
        tz=timezone.utc,
    ).isoformat()


def load_closed_15m(
    symbol: str,
    entry_candle_unix: int,
) -> List[Dict]:
    con = connect()

    try:
        rows = con.execute(
            """
            SELECT
                timestamp,
                timestamp_unix,
                open,
                high,
                low,
                close
            FROM market_snapshots
            WHERE symbol = ?
              AND timeframe = '15m'
              AND timestamp_unix >= ?
              AND open IS NOT NULL
              AND high IS NOT NULL
              AND low IS NOT NULL
              AND close IS NOT NULL
            ORDER BY timestamp_unix ASC
            """,
            (
                symbol,
                int(entry_candle_unix),
            ),
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        con.close()


def update_excursions(
    trade_id: int,
    mfe_r: float,
    mae_r: float,
) -> None:
    con = connect()

    try:
        con.execute(
            """
            UPDATE paper_trades
            SET mfe_r = ?,
                mae_r = ?
            WHERE id = ?
              AND status = 'OPEN'
            """,
            (
                float(mfe_r),
                float(mae_r),
                int(trade_id),
            ),
        )
        con.commit()

    finally:
        con.close()


def close_trade(
    *,
    trade_id: int,
    exit_price: float,
    result_r: float,
    exit_reason: str,
    exit_timestamp: str,
    exit_candle_unix: int,
    mfe_r: float,
    mae_r: float,
    entry_price: float,
    side: str,
) -> None:
    if side == "LONG":
        pnl_percent = (
            (exit_price - entry_price)
            / entry_price
            * 100.0
        )
    else:
        pnl_percent = (
            (entry_price - exit_price)
            / entry_price
            * 100.0
        )

    con = connect()

    try:
        con.execute(
            """
            UPDATE paper_trades
            SET status = 'CLOSED',
                exit_price = ?,
                pnl_percent = ?,
                result_r = ?,
                closed_timestamp = ?,
                exit_candle_unix = ?,
                exit_reason = ?,
                mfe_r = ?,
                mae_r = ?
            WHERE id = ?
              AND status = 'OPEN'
            """,
            (
                float(exit_price),
                float(pnl_percent),
                float(result_r),
                exit_timestamp,
                int(exit_candle_unix),
                exit_reason,
                float(mfe_r),
                float(mae_r),
                int(trade_id),
            ),
        )
        con.commit()

    finally:
        con.close()


def monitor_trade(trade: Dict) -> Dict:
    trade_id = int(trade["id"])
    symbol = str(trade["symbol"])
    model_version = str(trade["model_version"])
    side = str(trade["side"]).upper()

    entry_candle_unix = trade["entry_candle_unix"]

    if entry_candle_unix is None:
        return {
            "action": "ERROR",
            "trade_id": trade_id,
            "reason": "entry_candle_unix missing",
        }

    entry = float(trade["entry_price"])
    stop = float(trade["stop_loss"])
    tp = float(trade["tp1"])

    risk = abs(entry - stop)

    if risk <= 0:
        return {
            "action": "ERROR",
            "trade_id": trade_id,
            "reason": "invalid risk distance",
        }

    config = get_model_config(
        symbol,
        model_version,
    )

    max_bars = int(
        config.get("max_bars", 32)
    )

    tp_r = float(
        config["tp_r"]
    )

    rows = load_closed_15m(
        symbol,
        int(entry_candle_unix),
    )

    if not rows:
        return {
            "action": "WAIT",
            "trade_id": trade_id,
            "reason": "no closed 15m candles yet",
        }

    # Exactly like simulate_trade():
    # evaluate at most max_bars candles,
    # beginning with the entry candle itself.
    rows = rows[:max_bars]

    mfe_r = 0.0
    mae_r = 0.0

    for row in rows:
        high = float(row["high"])
        low = float(row["low"])

        if side == "LONG":
            stop_hit = low <= stop
            tp_hit = high >= tp
        elif side == "SHORT":
            stop_hit = high >= stop
            tp_hit = low <= tp
        else:
            return {
                "action": "ERROR",
                "trade_id": trade_id,
                "reason": f"invalid side: {side}",
            }

        # Conservative intrabar assumption:
        # STOP always wins if both levels were touched.
        if stop_hit:
            mae_r = max(
                mae_r,
                1.0,
            )

            close_trade(
                trade_id=trade_id,
                exit_price=stop,
                result_r=-1.0,
                exit_reason="STOP",
                exit_timestamp=row["timestamp"],
                exit_candle_unix=int(row["timestamp_unix"]),
                mfe_r=mfe_r,
                mae_r=mae_r,
                entry_price=entry,
                side=side,
            )

            return {
                "action": "CLOSED",
                "trade_id": trade_id,
                "exit_reason": "STOP",
                "exit_price": round(stop, 2),
                "result_r": -1.0,
                "mfe_r": round(mfe_r, 4),
                "mae_r": round(mae_r, 4),
                "bars_seen": len(rows),
            }

        if tp_hit:
            mfe_r = max(
                mfe_r,
                tp_r,
            )

            close_trade(
                trade_id=trade_id,
                exit_price=tp,
                result_r=tp_r,
                exit_reason="TP",
                exit_timestamp=row["timestamp"],
                exit_candle_unix=int(row["timestamp_unix"]),
                mfe_r=mfe_r,
                mae_r=mae_r,
                entry_price=entry,
                side=side,
            )

            return {
                "action": "CLOSED",
                "trade_id": trade_id,
                "exit_reason": "TP",
                "exit_price": round(tp, 2),
                "result_r": tp_r,
                "mfe_r": round(mfe_r, 4),
                "mae_r": round(mae_r, 4),
                "bars_seen": len(rows),
            }

        if side == "LONG":
            favorable_r = (
                high - entry
            ) / risk

            adverse_r = (
                entry - low
            ) / risk

        else:
            favorable_r = (
                entry - low
            ) / risk

            adverse_r = (
                high - entry
            ) / risk

        mfe_r = max(
            mfe_r,
            favorable_r,
            0.0,
        )

        mae_r = max(
            mae_r,
            adverse_r,
            0.0,
        )

    # TIME_EXIT only when all max_bars
    # have actually closed.
    if len(rows) >= max_bars:
        exit_row = rows[max_bars - 1]
        exit_price = float(
            exit_row["close"]
        )

        if side == "LONG":
            result_r = (
                exit_price - entry
            ) / risk
        else:
            result_r = (
                entry - exit_price
            ) / risk

        close_trade(
            trade_id=trade_id,
            exit_price=exit_price,
            result_r=result_r,
            exit_reason="TIME_EXIT",
            exit_timestamp=exit_row["timestamp"],
            exit_candle_unix=int(exit_row["timestamp_unix"]),
            mfe_r=mfe_r,
            mae_r=mae_r,
            entry_price=entry,
            side=side,
        )

        return {
            "action": "CLOSED",
            "trade_id": trade_id,
            "exit_reason": "TIME_EXIT",
            "exit_price": round(
                exit_price,
                2,
            ),
            "result_r": round(
                result_r,
                4,
            ),
            "mfe_r": round(
                mfe_r,
                4,
            ),
            "mae_r": round(
                mae_r,
                4,
            ),
            "bars_seen": max_bars,
        }

    update_excursions(
        trade_id,
        mfe_r,
        mae_r,
    )

    return {
        "action": "OPEN",
        "trade_id": trade_id,
        "bars_seen": len(rows),
        "bars_remaining": (
            max_bars - len(rows)
        ),
        "mfe_r": round(
            mfe_r,
            4,
        ),
        "mae_r": round(
            mae_r,
            4,
        ),
    }


def run_once() -> List[Dict]:
    trades = list_open_paper_trades()

    results = []

    for trade in trades:
        try:
            results.append(
                monitor_trade(trade)
            )
        except Exception as exc:
            results.append({
                "action": "ERROR",
                "trade_id": trade["id"],
                "error": str(exc),
            })

    return results


if __name__ == "__main__":
    print()
    print(
        "ORACLE X — PAPER POSITION MONITOR"
    )
    print("=" * 70)

    results = run_once()

    if not results:
        print("No open paper trades")

    for result in results:
        print()
        for key, value in result.items():
            print(f"{key}: {value}")
