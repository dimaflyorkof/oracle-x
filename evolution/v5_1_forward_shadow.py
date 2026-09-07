from __future__ import annotations

import argparse
import json
import time
from bisect import bisect_right
from dataclasses import asdict
from datetime import datetime, timezone
from statistics import median
from typing import Dict, List, Optional

import requests

from core.market_intelligence import analyze_market_intelligence
from core.global_intelligence_v2 import analyze_global_intelligence_v2
from database.db import connect
from evolution.backtest import load_rows
from evolution.flow_edge_search_v3 import (
    derivative_features,
    load_derivatives,
    load_orderflow,
    mean,
    safe_float,
)
from evolution.slow_edge_search_v5_1 import (
    Candidate,
    SignalPoint,
    aggregate_flow,
    build_atr,
    build_rsi,
    completed_4h_position,
    derivatives_blocked,
    flow_blocked,
    rolling_ema,
    setup_direction,
)


SYMBOL = "BTC"
MODEL_VERSION = "V5.1-FROZEN"
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
POLL_SECONDS = 60
MAX_SIGNAL_AGE_SECONDS = 2 * 3600
FEE_BPS = 4.0
SLIPPAGE_BPS = 1.0

FROZEN = Candidate(
    setup="BREAKOUT",
    stop_atr=2.5,
    tp_r=4.0,
    max_hold_hours=48,
    flow_threshold=0.03,
    trend_strength_min=0.5,
    cooldown_hours=1,
)

SPOT_SOURCE = "binance_spot_kline_15m"
FUTURES_SOURCE = "binance_futures_kline_15m"


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def init_shadow_schema() -> None:
    con = connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS shadow_v5_1_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_timestamp TEXT NOT NULL,
                created_unix INTEGER NOT NULL,
                signal_candle_unix INTEGER NOT NULL UNIQUE,
                decision_unix INTEGER NOT NULL,
                action TEXT NOT NULL,
                reason TEXT,
                side TEXT,
                point_json TEXT,
                intelligence_decision TEXT,
                intelligence_score REAL,
                intelligence_confidence REAL,
                intelligence_coverage REAL,
                intelligence_allowed INTEGER,
                intelligence_json TEXT,
                global_v2_decision TEXT,
                global_v2_score REAL,
                global_v2_confidence REAL,
                global_v2_coverage REAL,
                global_v2_allowed INTEGER,
                global_v2_json TEXT
            )
            """
        )
        evaluation_columns = {
            str(row["name"])
            for row in con.execute(
                "PRAGMA table_info(shadow_v5_1_evaluations)"
            ).fetchall()
        }
        required_evaluation_columns = (
            ("global_v2_decision", "TEXT"),
            ("global_v2_score", "REAL"),
            ("global_v2_confidence", "REAL"),
            ("global_v2_coverage", "REAL"),
            ("global_v2_allowed", "INTEGER"),
            ("global_v2_json", "TEXT"),
        )
        for name, column_type in required_evaluation_columns:
            if name not in evaluation_columns:
                con.execute(
                    f"ALTER TABLE shadow_v5_1_evaluations "
                    f"ADD COLUMN {name} {column_type}"
                )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS shadow_v5_1_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_version TEXT NOT NULL,
                created_timestamp TEXT NOT NULL,
                created_unix INTEGER NOT NULL,
                signal_candle_unix INTEGER NOT NULL UNIQUE,
                entry_candle_unix INTEGER NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                entry_price REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                atr_1h REAL NOT NULL,
                risk_price REAL NOT NULL,
                fee_bps REAL NOT NULL,
                slippage_bps REAL NOT NULL,
                cost_r REAL NOT NULL,
                global_allowed INTEGER,
                global_decision TEXT,
                global_score REAL,
                global_coverage REAL,
                global_v2_allowed INTEGER,
                global_v2_decision TEXT,
                global_v2_score REAL,
                global_v2_confidence REAL,
                global_v2_coverage REAL,
                global_v2_json TEXT,
                exit_price REAL,
                exit_reason TEXT,
                exit_candle_unix INTEGER,
                closed_timestamp TEXT,
                result_r REAL,
                mfe_r REAL NOT NULL DEFAULT 0.0,
                mae_r REAL NOT NULL DEFAULT 0.0,
                bars_seen INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        trade_columns = {
            str(row["name"])
            for row in con.execute(
                "PRAGMA table_info(shadow_v5_1_trades)"
            ).fetchall()
        }
        required_trade_columns = (
            ("global_v2_allowed", "INTEGER"),
            ("global_v2_decision", "TEXT"),
            ("global_v2_score", "REAL"),
            ("global_v2_confidence", "REAL"),
            ("global_v2_coverage", "REAL"),
            ("global_v2_json", "TEXT"),
        )
        for name, column_type in required_trade_columns:
            if name not in trade_columns:
                con.execute(
                    f"ALTER TABLE shadow_v5_1_trades "
                    f"ADD COLUMN {name} {column_type}"
                )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_shadow_v5_1_trade_status
            ON shadow_v5_1_trades (status, entry_candle_unix)
            """
        )
        con.commit()
    finally:
        con.close()


def latest_closed_1h(now_unix: int) -> Optional[int]:
    con = connect()
    try:
        row = con.execute(
            """
            SELECT MAX(timestamp_unix) AS ts
            FROM market_snapshots
            WHERE symbol = ?
              AND timeframe = '1h'
              AND timestamp_unix + 3600 <= ?
              AND open IS NOT NULL
              AND high IS NOT NULL
              AND low IS NOT NULL
              AND close IS NOT NULL
            """,
            (SYMBOL, int(now_unix)),
        ).fetchone()
        return int(row["ts"]) if row and row["ts"] is not None else None
    finally:
        con.close()


def was_evaluated(signal_ts: int) -> bool:
    con = connect()
    try:
        row = con.execute(
            "SELECT 1 FROM shadow_v5_1_evaluations "
            "WHERE signal_candle_unix = ? LIMIT 1",
            (int(signal_ts),),
        ).fetchone()
        return row is not None
    finally:
        con.close()


def open_trade() -> Optional[Dict]:
    con = connect()
    try:
        row = con.execute(
            "SELECT * FROM shadow_v5_1_trades "
            "WHERE status = 'OPEN' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def last_exit_available_unix() -> int:
    con = connect()
    try:
        row = con.execute(
            "SELECT exit_candle_unix FROM shadow_v5_1_trades "
            "WHERE status = 'CLOSED' AND exit_candle_unix IS NOT NULL "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return 0
        return int(row["exit_candle_unix"]) + 900
    finally:
        con.close()


def record_evaluation(
    signal_ts: int,
    action: str,
    reason: str,
    point: Optional[SignalPoint] = None,
    side: Optional[str] = None,
    intelligence=None,
    intelligence_allowed: Optional[bool] = None,
    intelligence_v2=None,
    intelligence_v2_allowed: Optional[bool] = None,
) -> bool:
    now = int(time.time())
    point_json = json.dumps(asdict(point), ensure_ascii=False) if point else None
    intelligence_json = (
        json.dumps(asdict(intelligence), ensure_ascii=False)
        if intelligence is not None
        else None
    )
    intelligence_v2_json = (
        json.dumps(asdict(intelligence_v2), ensure_ascii=False)
        if intelligence_v2 is not None
        else None
    )
    con = connect()
    try:
        cur = con.execute(
            """
            INSERT OR IGNORE INTO shadow_v5_1_evaluations (
                created_timestamp, created_unix,
                signal_candle_unix, decision_unix,
                action, reason, side, point_json,
                intelligence_decision, intelligence_score,
                intelligence_confidence, intelligence_coverage,
                intelligence_allowed, intelligence_json,
                global_v2_decision, global_v2_score,
                global_v2_confidence, global_v2_coverage,
                global_v2_allowed, global_v2_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                iso(now), now, int(signal_ts), int(signal_ts) + 3600,
                action, reason, side, point_json,
                getattr(intelligence, "decision", None),
                getattr(intelligence, "score", None),
                getattr(intelligence, "confidence", None),
                getattr(intelligence, "data_coverage", None),
                None if intelligence_allowed is None else int(intelligence_allowed),
                intelligence_json,
                getattr(intelligence_v2, "decision", None),
                getattr(intelligence_v2, "score", None),
                getattr(intelligence_v2, "confidence", None),
                getattr(intelligence_v2, "data_coverage", None),
                (
                    None
                    if intelligence_v2_allowed is None
                    else int(intelligence_v2_allowed)
                ),
                intelligence_v2_json,
            ),
        )
        con.commit()
        return cur.rowcount == 1
    finally:
        con.close()


def atr_ratio_at(index: int, rows_1h: list, atr_1h: List[Optional[float]]) -> float:
    current_atr = atr_1h[index]
    current_close = safe_float(rows_1h[index]["close"])
    if current_atr is None or current_close <= 0:
        return 1.0
    values = []
    for item in range(max(50, index - 336), index):
        atr_value = atr_1h[item]
        close = safe_float(rows_1h[item]["close"])
        if atr_value is not None and close > 0:
            values.append(float(atr_value) / close)
    current = float(current_atr) / current_close
    return current / max(median(values), 1e-12) if len(values) >= 48 else 1.0


def point_at(
    index: int,
    rows_1h: list,
    rows_4h: list,
    atr_1h: List[Optional[float]],
    rsi_1h: List[Optional[float]],
    ema20_1h: List[Optional[float]],
    atr_4h: List[Optional[float]],
    ema20_4h: List[Optional[float]],
    ema50_4h: List[Optional[float]],
    ts_4h: List[int],
    orderflow,
    derivatives,
) -> Optional[SignalPoint]:
    if index < 50:
        return None
    signal_ts = int(rows_1h[index]["timestamp_unix"])
    decision_ts = signal_ts + 3600
    position_4h = completed_4h_position(ts_4h, decision_ts)
    if position_4h < 50:
        return None
    required = (
        atr_1h[index], rsi_1h[index], ema20_1h[index],
        atr_4h[position_4h], ema20_4h[position_4h], ema50_4h[position_4h],
    )
    if any(value is None for value in required):
        return None
    close = safe_float(rows_1h[index]["close"])
    current_atr = float(atr_1h[index])
    if close <= 0 or current_atr <= 0:
        return None
    ema_gap = float(ema20_4h[position_4h]) - float(ema50_4h[position_4h])
    trend_atr = max(float(atr_4h[position_4h]), 1e-12)
    trend_direction = "LONG" if ema_gap > 0 else "SHORT" if ema_gap < 0 else "NONE"
    volumes = [safe_float(rows_1h[item]["volume"]) for item in range(index - 24, index)]
    spot_flow = aggregate_flow(orderflow[SPOT_SOURCE], signal_ts)
    futures_flow = aggregate_flow(orderflow[FUTURES_SOURCE], signal_ts)
    if spot_flow is None or futures_flow is None:
        return None
    funding, _, crowding, _, coverage = derivative_features(derivatives, decision_ts)
    return SignalPoint(
        index_1h=index,
        signal_open_ts=signal_ts,
        decision_ts=decision_ts,
        execution_start_15m=0,
        atr_1h=current_atr,
        close=close,
        ema20_1h=float(ema20_1h[index]),
        rsi_1h=float(rsi_1h[index]),
        trend_direction=trend_direction,
        trend_strength=abs(ema_gap) / trend_atr,
        atr_ratio=atr_ratio_at(index, rows_1h, atr_1h),
        volume_ratio=safe_float(rows_1h[index]["volume"]) / max(mean(volumes), 1e-12),
        prior_high_24h=max(safe_float(rows_1h[item]["high"]) for item in range(index - 24, index)),
        prior_low_24h=min(safe_float(rows_1h[item]["low"]) for item in range(index - 24, index)),
        spot_flow_1h=float(spot_flow),
        futures_flow_1h=float(futures_flow),
        funding_z=float(funding),
        crowding_z=float(crowding),
        derivatives_coverage=float(coverage),
    )


def latest_points(signal_ts: int) -> tuple[SignalPoint, SignalPoint]:
    rows_1h = load_rows(SYMBOL, "1h")
    rows_4h = load_rows(SYMBOL, "4h")
    indexes = {
        int(row["timestamp_unix"]): index
        for index, row in enumerate(rows_1h)
    }
    index = indexes.get(int(signal_ts))
    if index is None or index < 51:
        raise RuntimeError("Latest closed 1h signal candle is unavailable")
    atr_1h = build_atr(rows_1h)
    rsi_1h = build_rsi(rows_1h)
    ema20_1h = rolling_ema(rows_1h, 20)
    atr_4h = build_atr(rows_4h)
    ema20_4h = rolling_ema(rows_4h, 20)
    ema50_4h = rolling_ema(rows_4h, 50)
    ts_4h = [int(row["timestamp_unix"]) for row in rows_4h]
    orderflow = load_orderflow()
    derivatives = load_derivatives()
    current = point_at(
        index, rows_1h, rows_4h, atr_1h, rsi_1h, ema20_1h,
        atr_4h, ema20_4h, ema50_4h, ts_4h, orderflow, derivatives,
    )
    previous = point_at(
        index - 1, rows_1h, rows_4h, atr_1h, rsi_1h, ema20_1h,
        atr_4h, ema20_4h, ema50_4h, ts_4h, orderflow, derivatives,
    )
    if current is None or previous is None:
        raise RuntimeError("Could not build causal V5.1 signal points")
    return current, previous


def fetch_exact_entry_open(entry_unix: int) -> Optional[float]:
    response = requests.get(
        BINANCE_KLINES_URL,
        params={
            "symbol": "BTCUSDT",
            "interval": "15m",
            "startTime": int(entry_unix) * 1000,
            "limit": 1,
        },
        timeout=10,
    )
    response.raise_for_status()
    rows = response.json()
    if not rows or int(rows[0][0]) // 1000 != int(entry_unix):
        return None
    return float(rows[0][1])


def create_trade(
    point: SignalPoint,
    side: str,
    entry: float,
    intelligence,
    global_allowed: bool,
    intelligence_v2,
    global_v2_allowed: bool,
) -> Optional[int]:
    distance = point.atr_1h * FROZEN.stop_atr
    if distance <= 0:
        raise RuntimeError("Invalid frozen stop distance")
    if side == "LONG":
        stop = entry - distance
        take_profit = entry + distance * FROZEN.tp_r
    else:
        stop = entry + distance
        take_profit = entry - distance * FROZEN.tp_r
    risk_price = abs(entry - stop)
    round_trip_bps = 2.0 * (FEE_BPS + SLIPPAGE_BPS)
    cost_r = entry * (round_trip_bps / 10000.0) / risk_price
    now = int(time.time())
    con = connect()
    try:
        if con.execute(
            "SELECT 1 FROM shadow_v5_1_trades WHERE status='OPEN' LIMIT 1"
        ).fetchone():
            return None
        cur = con.execute(
            """
            INSERT OR IGNORE INTO shadow_v5_1_trades (
                model_version, created_timestamp, created_unix,
                signal_candle_unix, entry_candle_unix, side,
                entry_price, stop_loss, take_profit, atr_1h,
                risk_price, fee_bps, slippage_bps, cost_r,
                global_allowed, global_decision, global_score, global_coverage
                , global_v2_allowed, global_v2_decision,
                global_v2_score, global_v2_confidence,
                global_v2_coverage, global_v2_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                MODEL_VERSION, iso(now), now,
                point.signal_open_ts, point.decision_ts, side,
                entry, stop, take_profit, point.atr_1h,
                risk_price, FEE_BPS, SLIPPAGE_BPS, cost_r,
                int(global_allowed), intelligence.decision,
                intelligence.score, intelligence.data_coverage,
                int(global_v2_allowed), intelligence_v2.decision,
                intelligence_v2.score, intelligence_v2.confidence,
                intelligence_v2.data_coverage,
                json.dumps(asdict(intelligence_v2), ensure_ascii=False),
            ),
        )
        con.commit()
        return int(cur.lastrowid) if cur.rowcount == 1 else None
    finally:
        con.close()


def closed_15m_rows(entry_unix: int) -> List[Dict]:
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT timestamp, timestamp_unix, open, high, low, close
            FROM market_snapshots
            WHERE symbol = ? AND timeframe = '15m'
              AND timestamp_unix >= ?
              AND open IS NOT NULL AND high IS NOT NULL
              AND low IS NOT NULL AND close IS NOT NULL
            ORDER BY timestamp_unix ASC
            LIMIT ?
            """,
            (SYMBOL, int(entry_unix), FROZEN.max_hold_hours * 4),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()


def close_shadow_trade(
    trade_id: int,
    exit_price: float,
    exit_reason: str,
    exit_row: Dict,
    result_r: float,
    mfe_r: float,
    mae_r: float,
    bars_seen: int,
) -> None:
    con = connect()
    try:
        con.execute(
            """
            UPDATE shadow_v5_1_trades
            SET status='CLOSED', exit_price=?, exit_reason=?,
                exit_candle_unix=?, closed_timestamp=?, result_r=?,
                mfe_r=?, mae_r=?, bars_seen=?
            WHERE id=? AND status='OPEN'
            """,
            (
                float(exit_price), exit_reason,
                int(exit_row["timestamp_unix"]), str(exit_row["timestamp"]),
                float(result_r), float(mfe_r), float(mae_r),
                int(bars_seen), int(trade_id),
            ),
        )
        con.commit()
    finally:
        con.close()


def monitor_open_trade() -> Dict:
    trade = open_trade()
    if trade is None:
        return {"action": "NO_OPEN_TRADE"}
    rows = closed_15m_rows(int(trade["entry_candle_unix"]))
    if not rows:
        return {"action": "WAIT", "trade_id": trade["id"], "bars_seen": 0}
    side = str(trade["side"])
    entry = float(trade["entry_price"])
    stop = float(trade["stop_loss"])
    take_profit = float(trade["take_profit"])
    risk = float(trade["risk_price"])
    cost_r = float(trade["cost_r"])
    mfe_r = 0.0
    mae_r = 0.0
    for index, row in enumerate(rows, 1):
        high = float(row["high"])
        low = float(row["low"])
        if side == "LONG":
            stop_hit, tp_hit = low <= stop, high >= take_profit
        else:
            stop_hit, tp_hit = high >= stop, low <= take_profit
        if stop_hit:
            mae_r = max(mae_r, 1.0)
            result_r = -1.0 - cost_r
            close_shadow_trade(
                int(trade["id"]), stop, "STOP", row,
                result_r, mfe_r, mae_r, index,
            )
            return {"action": "CLOSED", "reason": "STOP", "result_r": round(result_r, 6)}
        if tp_hit:
            mfe_r = max(mfe_r, FROZEN.tp_r)
            result_r = FROZEN.tp_r - cost_r
            close_shadow_trade(
                int(trade["id"]), take_profit, "TP", row,
                result_r, mfe_r, mae_r, index,
            )
            return {"action": "CLOSED", "reason": "TP", "result_r": round(result_r, 6)}
        if side == "LONG":
            favorable = (high - entry) / risk
            adverse = (entry - low) / risk
        else:
            favorable = (entry - low) / risk
            adverse = (high - entry) / risk
        mfe_r = max(mfe_r, favorable, 0.0)
        mae_r = max(mae_r, adverse, 0.0)
    if len(rows) >= FROZEN.max_hold_hours * 4:
        row = rows[-1]
        exit_price = float(row["close"])
        gross_r = (
            (exit_price - entry) / risk
            if side == "LONG"
            else (entry - exit_price) / risk
        )
        result_r = gross_r - cost_r
        close_shadow_trade(
            int(trade["id"]), exit_price, "TIME_EXIT", row,
            result_r, mfe_r, mae_r, len(rows),
        )
        return {"action": "CLOSED", "reason": "TIME_EXIT", "result_r": round(result_r, 6)}
    con = connect()
    try:
        con.execute(
            "UPDATE shadow_v5_1_trades SET mfe_r=?, mae_r=?, bars_seen=? "
            "WHERE id=? AND status='OPEN'",
            (mfe_r, mae_r, len(rows), int(trade["id"])),
        )
        con.commit()
    finally:
        con.close()
    return {
        "action": "OPEN",
        "trade_id": trade["id"],
        "bars_seen": len(rows),
        "bars_remaining": FROZEN.max_hold_hours * 4 - len(rows),
        "mfe_r": round(mfe_r, 6),
        "mae_r": round(mae_r, 6),
    }


def evaluate_latest(now_unix: int) -> Dict:
    signal_ts = latest_closed_1h(now_unix)
    if signal_ts is None:
        return {"action": "WAIT", "reason": "NO_CLOSED_1H"}
    if was_evaluated(signal_ts):
        return {"action": "ALREADY_EVALUATED", "signal": iso(signal_ts)}
    decision_ts = signal_ts + 3600
    if now_unix - decision_ts > MAX_SIGNAL_AGE_SECONDS:
        record_evaluation(signal_ts, "STALE_SKIPPED", "Signal was not observed live")
        return {"action": "STALE_SKIPPED", "signal": iso(signal_ts)}
    active = open_trade()
    if active is not None:
        record_evaluation(signal_ts, "OPEN_POSITION", f"trade_id={active['id']}")
        return {"action": "OPEN_POSITION", "trade_id": active["id"]}
    next_allowed = last_exit_available_unix() + FROZEN.cooldown_hours * 3600
    if decision_ts < next_allowed:
        record_evaluation(signal_ts, "COOLDOWN", f"next_allowed_unix={next_allowed}")
        return {"action": "COOLDOWN", "next_allowed": iso(next_allowed)}
    current, previous = latest_points(signal_ts)
    direction = setup_direction(current, FROZEN, previous)
    if direction is None:
        record_evaluation(signal_ts, "NO_SIGNAL", "Frozen breakout conditions not met", current)
        return {"action": "NO_SIGNAL", "signal": iso(signal_ts)}
    if flow_blocked(current, direction, FROZEN.flow_threshold):
        record_evaluation(signal_ts, "FLOW_BLOCKED", "Frozen dual-flow veto", current, direction)
        return {"action": "FLOW_BLOCKED", "side": direction}
    if derivatives_blocked(current, direction):
        record_evaluation(signal_ts, "DERIVATIVES_BLOCKED", "Frozen derivatives veto", current, direction)
        return {"action": "DERIVATIVES_BLOCKED", "side": direction}
    intelligence = analyze_market_intelligence(SYMBOL, as_of_ts=decision_ts)
    intelligence_v2 = analyze_global_intelligence_v2(
        SYMBOL,
        as_of_ts=decision_ts,
    )
    required = "LONG_ALLOWED" if direction == "LONG" else "SHORT_ALLOWED"
    global_allowed = (
        intelligence.data_coverage >= 0.65
        and intelligence.decision == required
    )
    global_v2_allowed = (
        intelligence_v2.data_coverage >= 0.55
        and intelligence_v2.decision == required
    )
    entry = fetch_exact_entry_open(decision_ts)
    if entry is None:
        return {"action": "WAIT", "reason": "EXACT_ENTRY_OPEN_UNAVAILABLE"}
    trade_id = create_trade(
        current,
        direction,
        entry,
        intelligence,
        global_allowed,
        intelligence_v2,
        global_v2_allowed,
    )
    if trade_id is None:
        return {"action": "RACE_SKIPPED", "reason": "Trade already exists"}
    record_evaluation(
        signal_ts, "TRADE_OPENED", f"shadow_trade_id={trade_id}",
        current,
        direction,
        intelligence,
        global_allowed,
        intelligence_v2,
        global_v2_allowed,
    )
    return {
        "action": "TRADE_OPENED",
        "trade_id": trade_id,
        "side": direction,
        "entry": round(entry, 2),
        "global_allowed": global_allowed,
        "global_decision": intelligence.decision,
        "global_v2_allowed": global_v2_allowed,
        "global_v2_decision": intelligence_v2.decision,
    }


def summary() -> Dict:
    con = connect()
    try:
        rows = con.execute(
            "SELECT result_r, global_allowed, global_v2_allowed "
            "FROM shadow_v5_1_trades "
            "WHERE status='CLOSED' ORDER BY id"
        ).fetchall()
        open_count = int(con.execute(
            "SELECT COUNT(*) FROM shadow_v5_1_trades WHERE status='OPEN'"
        ).fetchone()[0])
    finally:
        con.close()
    values = [float(row["result_r"]) for row in rows]
    global_values = [
        float(row["result_r"])
        for row in rows if int(row["global_allowed"] or 0) == 1
    ]
    global_v2_values = [
        float(row["result_r"])
        for row in rows if int(row["global_v2_allowed"] or 0) == 1
    ]
    return {
        "closed_trades": len(values),
        "open_trades": open_count,
        "baseline_total_r": round(sum(values), 6),
        "global_allowed_trades": len(global_values),
        "global_overlay_total_r": round(sum(global_values), 6),
        "global_v2_allowed_trades": len(global_v2_values),
        "global_v2_overlay_total_r": round(sum(global_v2_values), 6),
    }


def run_once() -> Dict:
    init_shadow_schema()
    now = int(time.time())
    result = {
        "timestamp": iso(now),
        "monitor": monitor_open_trade(),
        "signal": evaluate_latest(now),
        "summary": summary(),
    }
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def run_forever() -> None:
    print("ORACLE X — V5.1 FROZEN FORWARD SHADOW", flush=True)
    print(json.dumps(asdict(FROZEN), ensure_ascii=False), flush=True)
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception as exc:
            print(json.dumps({
                "timestamp": iso(int(time.time())),
                "action": "ERROR",
                "error": f"{type(exc).__name__}: {exc}",
            }, ensure_ascii=False), flush=True)
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, POLL_SECONDS - elapsed))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        run_once()
    else:
        try:
            run_forever()
        except KeyboardInterrupt:
            print("Shadow runner stopped", flush=True)


if __name__ == "__main__":
    main()
