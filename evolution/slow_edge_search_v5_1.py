from __future__ import annotations

import json
import math
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Sequence, Tuple

from core.regime import ema
from evolution.backtest import load_rows, simulate_trade
from evolution.flow_edge_search_v3 import (
    derivative_features,
    load_derivatives,
    load_orderflow,
    mean,
    safe_float,
    trade_metrics,
)


SYMBOL = "BTC"
REPORT_PATH = Path("slow_edge_search_v5_1_result.json")
SELECTION_FEE_BPS = 4.0
SELECTION_SLIPPAGE_BPS = 1.0  # 10 bps round trip
STRESS_FEE_BPS = 4.0
STRESS_SLIPPAGE_BPS = 2.0  # 12 bps round trip
SPOT_SOURCE = "binance_spot_kline_15m"
FUTURES_SOURCE = "binance_futures_kline_15m"
EXPOSED_PERIOD_START_TS = 1772323200  # 2026-03-01T00:00:00Z


@dataclass(frozen=True)
class SignalPoint:
    index_1h: int
    signal_open_ts: int
    decision_ts: int
    execution_start_15m: int
    atr_1h: float
    close: float
    ema20_1h: float
    rsi_1h: float
    trend_direction: str
    trend_strength: float
    atr_ratio: float
    volume_ratio: float
    prior_high_24h: float
    prior_low_24h: float
    spot_flow_1h: float
    futures_flow_1h: float
    funding_z: float
    crowding_z: float
    derivatives_coverage: float


@dataclass(frozen=True)
class Candidate:
    setup: str
    stop_atr: float
    tp_r: float
    max_hold_hours: int
    flow_threshold: float
    trend_strength_min: float
    cooldown_hours: int = 1


@dataclass
class Evaluation:
    trades: list
    raw_signals: int
    flow_blocked: int
    derivatives_blocked: int


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def build_atr(rows: list, period: int = 14) -> List[Optional[float]]:
    result: List[Optional[float]] = []
    ranges: List[float] = []
    value: Optional[float] = None
    for index, row in enumerate(rows):
        high = safe_float(row["high"])
        low = safe_float(row["low"])
        previous_close = (
            safe_float(rows[index - 1]["close"])
            if index > 0
            else safe_float(row["open"])
        )
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        if len(ranges) < period:
            result.append(None)
            continue
        if value is None:
            value = mean(ranges[-period:])
        else:
            value = ((value * (period - 1)) + ranges[-1]) / period
        result.append(value)
    return result


def build_rsi(rows: list, period: int = 14) -> List[Optional[float]]:
    closes = [safe_float(row["close"]) for row in rows]
    result: List[Optional[float]] = [None] * len(rows)
    gains: List[float] = []
    losses: List[float] = []
    avg_gain: Optional[float] = None
    avg_loss: Optional[float] = None
    for index in range(1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
        if len(gains) < period:
            continue
        if avg_gain is None or avg_loss is None:
            avg_gain = mean(gains[-period:])
            avg_loss = mean(losses[-period:])
        else:
            avg_gain = ((avg_gain * (period - 1)) + gains[-1]) / period
            avg_loss = ((avg_loss * (period - 1)) + losses[-1]) / period
        if avg_loss <= 1e-12:
            result[index] = 100.0
        else:
            rs = avg_gain / avg_loss
            result[index] = 100.0 - 100.0 / (1.0 + rs)
    return result


def rolling_ema(rows: list, period: int) -> List[Optional[float]]:
    closes = [safe_float(row["close"]) for row in rows]
    return [ema(closes[: index + 1], period) for index in range(len(rows))]


def aggregate_flow(
    mapping: Dict[int, Tuple[float, float]],
    start_ts: int,
    bars: int = 4,
) -> Optional[float]:
    selected = [mapping.get(start_ts + offset * 900) for offset in range(bars)]
    if any(item is None for item in selected):
        return None
    weighted = sum(float(item[0]) * float(item[1]) for item in selected if item)
    total = sum(float(item[1]) for item in selected if item)
    return weighted / total if total > 0 else 0.0


def completed_4h_position(timestamps: Sequence[int], decision_ts: int) -> int:
    # A 4h candle is usable only after open + 4h <= decision time.
    return bisect_right(timestamps, decision_ts - 4 * 3600) - 1


def build_points(rows_1h: list, rows_4h: list, rows_15m: list) -> List[SignalPoint]:
    orderflow = load_orderflow()
    derivatives = load_derivatives()
    spot = orderflow[SPOT_SOURCE]
    futures = orderflow[FUTURES_SOURCE]

    atr_1h = build_atr(rows_1h)
    rsi_1h = build_rsi(rows_1h)
    ema20_1h = rolling_ema(rows_1h, 20)
    atr_4h = build_atr(rows_4h)
    ema20_4h = rolling_ema(rows_4h, 20)
    ema50_4h = rolling_ema(rows_4h, 50)
    ts_4h = [int(row["timestamp_unix"]) for row in rows_4h]
    ts_15m = [int(row["timestamp_unix"]) for row in rows_15m]
    atr_percent_history: List[float] = []
    points: List[SignalPoint] = []

    for index in range(50, len(rows_1h) - 2):
        signal_open_ts = int(rows_1h[index]["timestamp_unix"])
        decision_ts = signal_open_ts + 3600
        execution_position = bisect_left(ts_15m, decision_ts)
        if execution_position <= 0 or execution_position >= len(rows_15m):
            continue
        if ts_15m[execution_position] != decision_ts:
            continue

        position_4h = completed_4h_position(ts_4h, decision_ts)
        if position_4h < 50:
            continue
        current_atr = atr_1h[index]
        current_rsi = rsi_1h[index]
        current_ema20 = ema20_1h[index]
        trend_ema20 = ema20_4h[position_4h]
        trend_ema50 = ema50_4h[position_4h]
        trend_atr = atr_4h[position_4h]
        if any(value is None for value in (current_atr, current_rsi, current_ema20, trend_ema20, trend_ema50, trend_atr)):
            continue

        close = safe_float(rows_1h[index]["close"])
        if close <= 0 or current_atr is None or current_atr <= 0:
            continue
        atr_percent = float(current_atr) / close
        history = atr_percent_history[-24 * 14 :]
        atr_ratio = (
            atr_percent / max(median(history), 1e-12)
            if len(history) >= 48
            else 1.0
        )
        atr_percent_history.append(atr_percent)

        ema_gap = float(trend_ema20) - float(trend_ema50)
        trend_strength = abs(ema_gap) / max(float(trend_atr), 1e-12)
        if ema_gap > 0:
            trend_direction = "LONG"
        elif ema_gap < 0:
            trend_direction = "SHORT"
        else:
            trend_direction = "NONE"

        volumes = [safe_float(rows_1h[item]["volume"]) for item in range(index - 24, index)]
        volume_ratio = safe_float(rows_1h[index]["volume"]) / max(mean(volumes), 1e-12)
        prior_high = max(safe_float(rows_1h[item]["high"]) for item in range(index - 24, index))
        prior_low = min(safe_float(rows_1h[item]["low"]) for item in range(index - 24, index))
        spot_flow = aggregate_flow(spot, signal_open_ts)
        futures_flow = aggregate_flow(futures, signal_open_ts)
        if spot_flow is None or futures_flow is None:
            continue

        funding, _, crowding, _, coverage = derivative_features(derivatives, decision_ts)
        points.append(SignalPoint(
            index_1h=index,
            signal_open_ts=signal_open_ts,
            decision_ts=decision_ts,
            execution_start_15m=execution_position - 1,
            atr_1h=float(current_atr),
            close=close,
            ema20_1h=float(current_ema20),
            rsi_1h=float(current_rsi),
            trend_direction=trend_direction,
            trend_strength=trend_strength,
            atr_ratio=atr_ratio,
            volume_ratio=volume_ratio,
            prior_high_24h=prior_high,
            prior_low_24h=prior_low,
            spot_flow_1h=float(spot_flow),
            futures_flow_1h=float(futures_flow),
            funding_z=float(funding),
            crowding_z=float(crowding),
            derivatives_coverage=float(coverage),
        ))
    return points


def setup_direction(point: SignalPoint, candidate: Candidate, previous: SignalPoint) -> Optional[str]:
    if point.signal_open_ts - previous.signal_open_ts != 3600:
        return None
    if point.trend_direction == "NONE" or point.trend_strength < candidate.trend_strength_min:
        return None

    if candidate.setup == "TREND_PULLBACK":
        if point.atr_ratio > 1.50:
            return None
        if point.trend_direction == "LONG":
            reclaimed = previous.close <= previous.ema20_1h and point.close > point.ema20_1h
            momentum_turn = previous.rsi_1h < 45.0 <= point.rsi_1h
            return "LONG" if reclaimed or momentum_turn else None
        reclaimed = previous.close >= previous.ema20_1h and point.close < point.ema20_1h
        momentum_turn = previous.rsi_1h > 55.0 >= point.rsi_1h
        return "SHORT" if reclaimed or momentum_turn else None

    if candidate.setup == "BREAKOUT":
        if point.volume_ratio < 1.20 or point.atr_ratio < 0.80:
            return None
        if point.trend_direction == "LONG" and point.close > point.prior_high_24h:
            return "LONG"
        if point.trend_direction == "SHORT" and point.close < point.prior_low_24h:
            return "SHORT"
        return None

    raise ValueError(f"Unknown setup: {candidate.setup}")


def flow_blocked(point: SignalPoint, direction: str, threshold: float) -> bool:
    if direction == "LONG":
        return point.spot_flow_1h < -threshold and point.futures_flow_1h < -threshold
    return point.spot_flow_1h > threshold and point.futures_flow_1h > threshold


def derivatives_blocked(point: SignalPoint, direction: str) -> bool:
    if point.derivatives_coverage < 0.5:
        return False
    if direction == "LONG":
        return point.funding_z > 2.0 and point.crowding_z > 2.0
    return point.funding_z < -2.0 and point.crowding_z < -2.0


def evaluate(
    points: Sequence[SignalPoint],
    rows_15m: list,
    candidate: Candidate,
    start_ts: int,
    end_ts: int,
    fee_bps: float,
    slippage_bps: float,
) -> Evaluation:
    trades = []
    raw_signals = 0
    flow_vetoes = 0
    derivative_vetoes = 0
    next_allowed_ts = 0

    for index, point in enumerate(points):
        if point.signal_open_ts < start_ts:
            continue
        if point.signal_open_ts >= end_ts:
            break
        if index == 0 or point.decision_ts < next_allowed_ts:
            continue
        direction = setup_direction(point, candidate, points[index - 1])
        if direction is None:
            continue
        raw_signals += 1
        if flow_blocked(point, direction, candidate.flow_threshold):
            flow_vetoes += 1
            continue
        if derivatives_blocked(point, direction):
            derivative_vetoes += 1
            continue

        trade = simulate_trade(
            rows_15m,
            point.execution_start_15m,
            direction,
            point.atr_1h,
            stop_atr=candidate.stop_atr,
            tp_r=candidate.tp_r,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            max_bars=candidate.max_hold_hours * 4,
        )
        if trade is None:
            continue
        exit_ts = int(rows_15m[trade.exit_index]["timestamp_unix"]) + 900
        if exit_ts > end_ts:
            continue
        trades.append(trade)
        next_allowed_ts = exit_ts + candidate.cooldown_hours * 3600

    return Evaluation(trades, raw_signals, flow_vetoes, derivative_vetoes)


def evaluation_dict(value: Evaluation) -> dict:
    result = asdict(trade_metrics(value.trades))
    result.update({
        "raw_signals": value.raw_signals,
        "flow_blocked": value.flow_blocked,
        "derivatives_blocked": value.derivatives_blocked,
    })
    return result


def combined(values: Sequence[Evaluation]) -> dict:
    trades = [trade for value in values for trade in value.trades]
    return asdict(trade_metrics(trades))


def main() -> None:
    print("ORACLE X — SLOW 1H/4H EDGE SEARCH V5.1", flush=True)
    print("Closed 1h signal | closed 4h direction | next 1h open", flush=True)
    print("15m execution | selection 10 bps | stress 12 bps", flush=True)
    rows_15m = load_rows(SYMBOL, "15m")
    rows_1h = load_rows(SYMBOL, "1h")
    rows_4h = load_rows(SYMBOL, "4h")
    points = build_points(rows_1h, rows_4h, rows_15m)
    if len(points) < 2000:
        raise RuntimeError(f"Not enough complete hourly points: {len(points)}")
    timestamps = [point.signal_open_ts for point in points]
    research_points = [
        point
        for point in points
        if point.signal_open_ts < EXPOSED_PERIOD_START_TS
    ]
    if len(research_points) < 10000:
        raise RuntimeError(
            f"Not enough pre-exposure research points: {len(research_points)}"
        )
    research_timestamps = [point.signal_open_ts for point in research_points]
    boundaries = [
        research_timestamps[int(len(research_timestamps) * fraction)]
        for fraction in (0.40, 0.55, 0.70, 0.85)
    ]
    research_folds = list(zip(
        boundaries,
        boundaries[1:] + [EXPOSED_PERIOD_START_TS],
    ))
    final_ts = timestamps[-1] + 3600
    print(f"Points: {len(points)}", flush=True)
    print(f"Coverage: {iso(timestamps[0])} -> {iso(final_ts)}", flush=True)
    print(
        f"Research-only selection: {iso(timestamps[0])} -> "
        f"{iso(EXPOSED_PERIOD_START_TS)}",
        flush=True,
    )
    print(
        f"Previously exposed diagnostic: {iso(EXPOSED_PERIOD_START_TS)} -> "
        f"{iso(final_ts)}",
        flush=True,
    )

    candidates = [
        Candidate(setup, stop_atr, tp_r, hold, flow_threshold, trend_min)
        for setup in ("TREND_PULLBACK", "BREAKOUT")
        for stop_atr in (1.5, 2.0, 2.5)
        for tp_r in (2.0, 3.0, 4.0)
        for hold in (12, 24, 48)
        for flow_threshold in (0.03, 0.08)
        for trend_min in (0.25, 0.50)
    ]
    print(f"Bounded hypotheses: {len(candidates)}", flush=True)
    ranked = []
    for index, candidate in enumerate(candidates, 1):
        research_evaluations = [
            evaluate(points, rows_15m, candidate, start, end, SELECTION_FEE_BPS, SELECTION_SLIPPAGE_BPS)
            for start, end in research_folds
        ]
        selection = research_evaluations[:3]
        metrics = [trade_metrics(value.trades) for value in selection]
        if any(item.trades < 5 for item in metrics):
            score = -999.0
        else:
            worst_avg = min(item.average_r for item in metrics)
            worst_pf = min(item.profit_factor for item in metrics)
            dispersion = max(item.average_r for item in metrics) - worst_avg
            score = worst_avg + 0.10 * (worst_pf - 1.0) - 0.25 * dispersion
        ranked.append((score, candidate, research_evaluations))
        if index % 36 == 0:
            print(f"Evaluated {index}/{len(candidates)}", flush=True)

    ranked.sort(key=lambda item: item[0], reverse=True)
    winner_score, winner, winner_research_evaluations = ranked[0]
    research_holdout = evaluation_dict(winner_research_evaluations[3])
    exposed_diagnostic_evaluation = evaluate(
        points,
        rows_15m,
        winner,
        EXPOSED_PERIOD_START_TS,
        final_ts,
        SELECTION_FEE_BPS,
        SELECTION_SLIPPAGE_BPS,
    )
    exposed_diagnostic = evaluation_dict(exposed_diagnostic_evaluation)
    selection_combined = combined(winner_research_evaluations[:3])
    all_research_combined = combined(winner_research_evaluations)
    research_stress_evaluations = [
        evaluate(points, rows_15m, winner, start, end, STRESS_FEE_BPS, STRESS_SLIPPAGE_BPS)
        for start, end in research_folds
    ]
    research_stress = combined(research_stress_evaluations)
    selection_metrics = [
        trade_metrics(value.trades)
        for value in winner_research_evaluations[:3]
    ]
    gate = (
        selection_combined["trades"] >= 40
        and selection_combined["average_r"] >= 0.08
        and selection_combined["profit_factor"] >= 1.20
        and all(item.total_r > 0 for item in selection_metrics)
        and research_holdout["trades"] >= 12
        and research_holdout["total_r"] > 0
        and research_holdout["profit_factor"] >= 1.10
        and research_stress["total_r"] > 0
        and research_stress["profit_factor"] >= 1.10
        and research_stress["max_drawdown_r"] <= 12.0
        and exposed_diagnostic["trades"] >= 15
        and exposed_diagnostic["total_r"] > 0
        and exposed_diagnostic["profit_factor"] >= 1.05
    )
    status = "CANDIDATE_FOR_FORWARD_SHADOW" if gate else "NO_EDGE"

    report = {
        "status": status,
        "warning": "This is the one allowed evaluation on newly added old history. Only future shadow data can authorize paper or live trading.",
        "method": "Fixed 1h setups, fully closed 4h trend, next-hour entry, 15m execution, causal flow and derivatives vetoes",
        "selection_cost": "10 bps round trip",
        "stress_cost": "12 bps round trip",
        "evaluated_hypotheses": len(candidates),
        "coverage": {
            "start": iso(timestamps[0]),
            "research_end": iso(EXPOSED_PERIOD_START_TS),
            "exposed_diagnostic_end": iso(final_ts),
        },
        "winner": asdict(winner),
        "winner_score": round(winner_score, 6),
        "selection_folds": [
            {**evaluation_dict(value), "start": iso(start), "end": iso(end)}
            for value, (start, end) in zip(
                winner_research_evaluations[:3],
                research_folds[:3],
            )
        ],
        "selection_combined_at_10bps": selection_combined,
        "new_history_holdout_at_10bps": research_holdout,
        "all_new_history_at_10bps": all_research_combined,
        "all_new_history_stress_at_12bps": research_stress,
        "previously_exposed_2026_diagnostic_at_10bps": exposed_diagnostic,
        "historical_gate_passed": gate,
        "top_candidates": [
            {
                "score": round(score, 6),
                "candidate": asdict(candidate),
                "selection": combined(evaluations[:3]),
                "new_history_holdout": evaluation_dict(evaluations[3]),
                "previously_exposed_diagnostic": evaluation_dict(evaluate(
                    points,
                    rows_15m,
                    candidate,
                    EXPOSED_PERIOD_START_TS,
                    final_ts,
                    SELECTION_FEE_BPS,
                    SELECTION_SLIPPAGE_BPS,
                )),
            }
            for score, candidate, evaluations in ranked[:12]
        ],
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"Report: {REPORT_PATH}", flush=True)


if __name__ == "__main__":
    main()
