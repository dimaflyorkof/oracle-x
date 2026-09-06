from __future__ import annotations

import json
import math
from bisect import bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from database.db import connect
from core.market_intelligence import completed_hourly_derivative_rows
from evolution.backtest import load_rows, simulate_trade


SYMBOL = "BTC"
PRIMARY_FEE_BPS = 3.0
PRIMARY_SLIPPAGE_BPS = 1.0
REPORT_PATH = Path("flow_edge_search_v3_result.json")
SPOT_SOURCE = "binance_spot_kline_15m"
FUTURES_SOURCE = "binance_futures_kline_15m"
FEATURE_NAMES = (
    "return_1",
    "return_4",
    "return_16",
    "return_96",
    "atr_percent",
    "spot_flow_1h",
    "spot_flow_4h",
    "spot_flow_24h",
    "futures_flow_1h",
    "futures_flow_4h",
    "futures_flow_24h",
    "spot_futures_divergence_1h",
    "spot_futures_divergence_4h",
    "spot_flow_impulse",
    "futures_flow_impulse",
    "spot_volume_intensity",
    "futures_volume_intensity",
    "funding_causal_z",
    "oi_change_4h_causal_z",
    "crowding_causal_z",
    "derivatives_taker_causal_z",
    "derivatives_coverage",
)


@dataclass(frozen=True)
class FeaturePoint:
    index: int
    timestamp_unix: int
    values: Tuple[float, ...]


@dataclass(frozen=True)
class Candidate:
    horizon: int
    top_k: int
    signal_quantile: float
    threshold: float
    stop_atr: float
    tp_r: float
    direction: str
    cooldown_bars: int = 1


@dataclass
class Metrics:
    trades: int
    wins: int
    win_rate: float
    total_r: float
    average_r: float
    profit_factor: float
    max_drawdown_r: float


@dataclass
class Evaluation:
    metrics: Metrics
    signals: int


def iso(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def safe_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    result = float(value)
    return result if math.isfinite(result) else default


def mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def stdev(values: Sequence[float], center: Optional[float] = None) -> float:
    if len(values) < 2:
        return 1.0
    center = mean(values) if center is None else center
    variance = sum((value - center) ** 2 for value in values) / len(values)
    result = math.sqrt(max(variance, 0.0))
    return result if result > 1e-12 else 1.0


def quantile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = fraction * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def load_orderflow() -> Dict[str, Dict[int, Tuple[float, float]]]:
    result: Dict[str, Dict[int, Tuple[float, float]]] = {
        SPOT_SOURCE: {},
        FUTURES_SOURCE: {},
    }
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT id, timestamp_unix, source, buy_volume, sell_volume
            FROM orderflow_history
            WHERE symbol = ?
              AND source IN (?, ?)
              AND buy_volume IS NOT NULL
              AND sell_volume IS NOT NULL
            ORDER BY id ASC
            """,
            (SYMBOL, SPOT_SOURCE, FUTURES_SOURCE),
        ).fetchall()
    finally:
        con.close()
    for row in rows:
        buy = max(0.0, safe_float(row["buy_volume"]))
        sell = max(0.0, safe_float(row["sell_volume"]))
        total = buy + sell
        imbalance = (buy - sell) / total if total > 0 else 0.0
        result[str(row["source"])][int(row["timestamp_unix"])] = (
            imbalance,
            total,
        )
    return result


def causal_zscores(values: Sequence[Optional[float]], window: int = 720) -> List[float]:
    result = []
    for index, current in enumerate(values):
        history = [
            float(value)
            for value in values[max(0, index - window) : index]
            if value is not None
        ]
        if current is None or len(history) < 72:
            result.append(0.0)
            continue
        center = mean(history)
        scale = stdev(history, center)
        zscore = (float(current) - center) / scale
        result.append(max(-5.0, min(5.0, zscore)))
    return result


def normalize_derivative_rows(timestamps: List[int], rows: List[dict]) -> None:
    funding_raw: List[Optional[float]] = []
    oi_change_raw: List[Optional[float]] = []
    crowding_raw: List[Optional[float]] = []
    taker_raw: List[Optional[float]] = []
    last_funding: Optional[float] = None
    for index, row in enumerate(rows):
        if row.get("funding_rate") is not None:
            last_funding = safe_float(row["funding_rate"])
        funding_raw.append(last_funding)
        current_oi = safe_float(row.get("open_interest"))
        old_position = bisect_right(timestamps, timestamps[index] - 4 * 3600) - 1
        old_oi = (
            safe_float(rows[old_position].get("open_interest"))
            if old_position >= 0
            else 0.0
        )
        oi_change_raw.append(
            current_oi / old_oi - 1.0
            if current_oi > 0 and old_oi > 0
            else None
        )
        ratio = safe_float(row.get("long_short_ratio"))
        crowding_raw.append(math.log(ratio) if ratio > 0 else None)
        taker_ratio = safe_float(row.get("taker_ratio"))
        taker_raw.append(math.log(taker_ratio) if taker_ratio > 0 else None)
    normalized_columns = {
        "funding_z": causal_zscores(funding_raw),
        "oi_change_z": causal_zscores(oi_change_raw),
        "crowding_z": causal_zscores(crowding_raw),
        "taker_z": causal_zscores(taker_raw),
    }
    for key, values in normalized_columns.items():
        for row, value in zip(rows, values):
            row[key] = value


def load_derivatives() -> Dict[str, Tuple[List[int], List[dict]]]:
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT id, timestamp_unix, event_timestamp_unix,
                   available_at_unix, source, funding_rate,
                   open_interest, open_interest_change,
                   long_short_ratio, taker_ratio, futures_basis
            FROM derivatives_history
            WHERE symbol = ?
              AND source IN ('binance_futures', 'bybit_futures', 'okx_futures')
              AND available_at_unix IS NOT NULL
            ORDER BY source ASC, available_at_unix ASC, id ASC
            """,
            (SYMBOL,),
        ).fetchall()
    finally:
        con.close()
    by_source: Dict[str, List[dict]] = {}
    for row in rows:
        source = str(row["source"])
        by_source.setdefault(source, []).append(dict(row))
    result = {}
    for source, source_rows in by_source.items():
        final_available = max(
            int(row["available_at_unix"])
            for row in source_rows
        )
        ordered_rows = completed_hourly_derivative_rows(
            source_rows,
            final_available + 3600,
            limit=100000,
        )
        timestamps = [int(row["available_at_unix"]) for row in ordered_rows]
        normalize_derivative_rows(timestamps, ordered_rows)
        result[source] = (timestamps, ordered_rows)
        print(
            f"Derivatives {source}: hourly_points={len(ordered_rows)}",
            flush=True,
        )
    return result


def latest_row(
    series: Optional[Tuple[List[int], List[dict]]],
    as_of_ts: int,
    max_age: int,
) -> Optional[dict]:
    if series is None:
        return None
    timestamps, rows = series
    position = bisect_right(timestamps, int(as_of_ts)) - 1
    if position < 0 or as_of_ts - timestamps[position] > max_age:
        return None
    return rows[position]


def rolling_mean(values: Sequence[Optional[float]], end: int, window: int) -> Optional[float]:
    start = end - window + 1
    if start < 0:
        return None
    selected = values[start : end + 1]
    clean = [float(value) for value in selected if value is not None]
    if len(clean) < max(1, int(window * 0.90)):
        return None
    return mean(clean)


def build_atr(rows: List) -> List[Optional[float]]:
    true_ranges: List[float] = []
    result: List[Optional[float]] = []
    for index, row in enumerate(rows):
        high = safe_float(row["high"])
        low = safe_float(row["low"])
        previous_close = (
            safe_float(rows[index - 1]["close"])
            if index > 0
            else safe_float(row["open"])
        )
        true_ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
        result.append(mean(true_ranges[-14:]) if len(true_ranges) >= 14 else None)
    return result


def derivative_features(
    derivatives: Dict[str, Tuple[List[int], List[dict]]],
    as_of_ts: int,
) -> Tuple[float, float, float, float, float]:
    funding = []
    oi_changes = []
    crowding = []
    taker = []
    active_primary_sources = 0
    for source in ("binance_futures", "bybit_futures", "okx_futures"):
        series = derivatives.get(source)
        current = latest_row(series, as_of_ts, 3 * 3600)
        if current is None:
            continue
        funding.append(safe_float(current.get("funding_z")))
        if source not in {"binance_futures", "bybit_futures"}:
            continue
        active_primary_sources += 1
        oi_changes.append(safe_float(current.get("oi_change_z")))
        crowding.append(safe_float(current.get("crowding_z")))
        taker.append(safe_float(current.get("taker_z")))
    return (
        mean(funding),
        mean(oi_changes),
        mean(crowding),
        mean(taker),
        active_primary_sources / 2.0,
    )


def build_feature_points(rows: List) -> Tuple[List[FeaturePoint], List[Optional[float]]]:
    orderflow = load_orderflow()
    derivatives = load_derivatives()
    spot_map = orderflow[SPOT_SOURCE]
    futures_map = orderflow[FUTURES_SOURCE]
    spot_flow: List[Optional[float]] = []
    futures_flow: List[Optional[float]] = []
    spot_total: List[Optional[float]] = []
    futures_total: List[Optional[float]] = []
    for row in rows:
        ts = int(row["timestamp_unix"])
        spot = spot_map.get(ts)
        futures = futures_map.get(ts)
        spot_flow.append(spot[0] if spot else None)
        spot_total.append(spot[1] if spot else None)
        futures_flow.append(futures[0] if futures else None)
        futures_total.append(futures[1] if futures else None)
    atr = build_atr(rows)
    points = []
    for index in range(96, len(rows) - 2):
        close = safe_float(rows[index]["close"])
        if close <= 0 or atr[index] is None or atr[index] <= 0:
            continue
        sf1 = rolling_mean(spot_flow, index, 4)
        sf4 = rolling_mean(spot_flow, index, 16)
        sf24 = rolling_mean(spot_flow, index, 96)
        ff1 = rolling_mean(futures_flow, index, 4)
        ff4 = rolling_mean(futures_flow, index, 16)
        ff24 = rolling_mean(futures_flow, index, 96)
        st1 = rolling_mean(spot_total, index, 4)
        st24 = rolling_mean(spot_total, index, 96)
        ft1 = rolling_mean(futures_total, index, 4)
        ft24 = rolling_mean(futures_total, index, 96)
        required = (sf1, sf4, sf24, ff1, ff4, ff24, st1, st24, ft1, ft24)
        if any(value is None for value in required):
            continue
        timestamp_unix = int(rows[index]["timestamp_unix"])
        funding, oi_change, crowding, taker, coverage = derivative_features(
            derivatives,
            timestamp_unix + 15 * 60,
        )
        values = (
            close / safe_float(rows[index - 1]["close"]) - 1.0,
            close / safe_float(rows[index - 4]["close"]) - 1.0,
            close / safe_float(rows[index - 16]["close"]) - 1.0,
            close / safe_float(rows[index - 96]["close"]) - 1.0,
            atr[index] / close,
            float(sf1),
            float(sf4),
            float(sf24),
            float(ff1),
            float(ff4),
            float(ff24),
            float(sf1) - float(ff1),
            float(sf4) - float(ff4),
            float(sf1) - float(sf24),
            float(ff1) - float(ff24),
            math.log(max(float(st1), 1.0) / max(float(st24), 1.0)),
            math.log(max(float(ft1), 1.0) / max(float(ft24), 1.0)),
            funding,
            oi_change,
            crowding,
            taker,
            coverage,
        )
        if all(math.isfinite(value) for value in values):
            points.append(FeaturePoint(index, timestamp_unix, values))
    return points, atr


def standardizer(points: Sequence[FeaturePoint]) -> Tuple[List[float], List[float]]:
    columns = list(zip(*(point.values for point in points)))
    centers = [mean(column) for column in columns]
    scales = [stdev(column, center) for column, center in zip(columns, centers)]
    return centers, scales


def standardized(point: FeaturePoint, centers: Sequence[float], scales: Sequence[float]) -> List[float]:
    return [
        (value - center) / scale
        for value, center, scale in zip(point.values, centers, scales)
    ]


def target(rows: List, atr: Sequence[Optional[float]], point: FeaturePoint, horizon: int) -> float:
    entry = safe_float(rows[point.index + 1]["open"])
    exit_price = safe_float(rows[point.index + horizon]["close"])
    risk_unit = safe_float(atr[point.index])
    if entry <= 0 or risk_unit <= 0:
        return 0.0
    return max(-3.0, min(3.0, (exit_price - entry) / risk_unit))


def correlation_weights(
    rows: List,
    atr: Sequence[Optional[float]],
    fit_points: Sequence[FeaturePoint],
    centers: Sequence[float],
    scales: Sequence[float],
    horizon: int,
    top_k: int,
) -> List[float]:
    midpoint = len(fit_points) // 2
    halves = (fit_points[:midpoint], fit_points[midpoint:])
    half_weights = []
    for subset in halves:
        ys = [target(rows, atr, point, horizon) for point in subset]
        y_center = mean(ys)
        y_scale = stdev(ys, y_center)
        weights = []
        for feature_index in range(len(FEATURE_NAMES)):
            covariance = 0.0
            for point, y_value in zip(subset, ys):
                x_value = (point.values[feature_index] - centers[feature_index]) / scales[feature_index]
                covariance += x_value * ((y_value - y_center) / y_scale)
            weights.append(covariance / max(1, len(subset)))
        half_weights.append(weights)
    stable = []
    for first, second in zip(*half_weights):
        if first * second <= 0:
            stable.append(0.0)
        else:
            stable.append(math.copysign(min(abs(first), abs(second)), first))
    selected = sorted(range(len(stable)), key=lambda i: abs(stable[i]), reverse=True)[:top_k]
    result = [0.0] * len(stable)
    norm = sum(abs(stable[index]) for index in selected)
    if norm <= 0:
        return result
    cap = 0.30
    remaining = {
        index
        for index in selected
        if abs(stable[index]) > 0
    }
    assigned: Dict[int, float] = {}
    remaining_mass = 1.0
    while remaining and remaining_mass > 1e-12:
        remaining_norm = sum(abs(stable[index]) for index in remaining)
        if remaining_norm <= 0:
            break
        proposed = {
            index: remaining_mass * abs(stable[index]) / remaining_norm
            for index in remaining
        }
        capped = {
            index
            for index, value in proposed.items()
            if value > cap
        }
        if not capped:
            assigned.update(proposed)
            break
        for index in capped:
            assigned[index] = cap
            remaining_mass -= cap
            remaining.remove(index)
    for index, magnitude in assigned.items():
        result[index] = math.copysign(magnitude, stable[index])
    return result


def predictions(
    points: Sequence[FeaturePoint],
    centers: Sequence[float],
    scales: Sequence[float],
    weights: Sequence[float],
) -> Dict[int, float]:
    result = {}
    for point in points:
        z = standardized(point, centers, scales)
        result[point.index] = sum(value * weight for value, weight in zip(z, weights))
    return result


def trade_metrics(trades: Sequence) -> Metrics:
    results = [float(trade.result_r) for trade in trades]
    wins = sum(value > 0 for value in results)
    gross_profit = sum(value for value in results if value > 0)
    gross_loss = abs(sum(value for value in results if value < 0))
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in results:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    count = len(results)
    return Metrics(
        trades=count,
        wins=wins,
        win_rate=round(wins / count * 100.0 if count else 0.0, 2),
        total_r=round(sum(results), 4),
        average_r=round(sum(results) / count if count else 0.0, 6),
        profit_factor=round(gross_profit / gross_loss if gross_loss else gross_profit, 4),
        max_drawdown_r=round(max_drawdown, 4),
    )


def evaluate(
    points: Sequence[FeaturePoint],
    rows: List,
    atr: Sequence[Optional[float]],
    prediction_by_index: Dict[int, float],
    candidate: Candidate,
    start_ts: int,
    end_ts: int,
    fee_bps: float,
    slippage_bps: float,
) -> Evaluation:
    trades = []
    signals = 0
    next_allowed_index = 0
    for point in points:
        if point.timestamp_unix < start_ts:
            continue
        if point.timestamp_unix >= end_ts:
            break
        if point.index < next_allowed_index:
            continue
        prediction = prediction_by_index.get(point.index, 0.0)
        if abs(prediction) < candidate.threshold:
            continue
        direction = "LONG" if prediction > 0 else "SHORT"
        if candidate.direction != "BOTH" and direction != candidate.direction:
            continue
        signals += 1
        atr_value = atr[point.index]
        if atr_value is None or atr_value <= 0:
            continue
        trade = simulate_trade(
            rows,
            point.index,
            direction,
            atr_value,
            stop_atr=candidate.stop_atr,
            tp_r=candidate.tp_r,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            max_bars=candidate.horizon,
        )
        if trade is None:
            continue
        exit_ts = int(rows[trade.exit_index]["timestamp_unix"]) + 900
        if exit_ts > end_ts:
            continue
        trades.append(trade)
        next_allowed_index = trade.exit_index + 1 + candidate.cooldown_bars
    return Evaluation(trade_metrics(trades), signals)


def rank_score(first: Metrics, second: Metrics) -> float:
    edge = min(first.average_r, second.average_r)
    pf = min(first.profit_factor, second.profit_factor)
    instability = abs(first.average_r - second.average_r)
    dd = first.max_drawdown_r / max(1, first.trades) + second.max_drawdown_r / max(1, second.trades)
    return edge + 0.12 * (pf - 1.0) - 0.30 * instability - 0.04 * dd


def evaluation_dict(value: Evaluation) -> dict:
    result = asdict(value.metrics)
    result["signals"] = value.signals
    return result


def main() -> None:
    print("ORACLE X — NATIVE FLOW EDGE SEARCH V3", flush=True)
    print("Available-at contract + completed-hour source parity", flush=True)
    print("Primary selection cost: 8 bps round trip", flush=True)
    rows = load_rows(SYMBOL, "15m")
    print("Building no-lookahead flow features...", flush=True)
    points, atr = build_feature_points(rows)
    if len(points) < 5000:
        raise RuntimeError(f"Not enough complete feature points: {len(points)}")
    timestamps = [point.timestamp_unix for point in points]
    start_ts = timestamps[0]
    fit_end = timestamps[int(len(points) * 0.50)]
    validation_1_end = timestamps[int(len(points) * 0.65)]
    validation_2_end = timestamps[int(len(points) * 0.80)]
    final_ts = int(rows[-1]["timestamp_unix"]) + 900
    fit_points = [
        point for point in points
        if point.timestamp_unix < fit_end - 32 * 900
    ]
    centers, scales = standardizer(fit_points)
    print(f"Complete points: {len(points)}", flush=True)
    print(f"Fit: {iso(start_ts)} -> {iso(fit_end)}", flush=True)
    print(f"Validation 1: {iso(fit_end)} -> {iso(validation_1_end)}", flush=True)
    print(f"Validation 2: {iso(validation_1_end)} -> {iso(validation_2_end)}", flush=True)
    print(f"Untouched test: {iso(validation_2_end)} -> {iso(final_ts)}", flush=True)
    ranked = []
    model_reports = []
    evaluated = 0
    for horizon in (4, 8, 16, 32):
        horizon_fit_points = [
            point for point in fit_points
            if point.timestamp_unix + (horizon + 1) * 900 <= fit_end
        ]
        for top_k in (4, 8, 12):
            weights = correlation_weights(
                rows, atr, horizon_fit_points, centers, scales, horizon, top_k
            )
            active_features = [
                {"name": FEATURE_NAMES[index], "weight": round(weight, 6)}
                for index, weight in enumerate(weights)
                if weight != 0.0
            ]
            prediction_by_index = predictions(points, centers, scales, weights)
            fit_abs_predictions = [
                abs(prediction_by_index[point.index])
                for point in horizon_fit_points
            ]
            model_reports.append(
                {
                    "horizon": horizon,
                    "top_k": top_k,
                    "active_features": active_features,
                }
            )
            for signal_quantile in (0.80, 0.875, 0.925):
                threshold = quantile(fit_abs_predictions, signal_quantile)
                for stop_atr in (1.5, 2.0, 2.5):
                    for tp_r in (1.5, 2.5, 4.0):
                        for direction in ("BOTH", "LONG", "SHORT"):
                            candidate = Candidate(
                                horizon=horizon,
                                top_k=top_k,
                                signal_quantile=signal_quantile,
                                threshold=threshold,
                                stop_atr=stop_atr,
                                tp_r=tp_r,
                                direction=direction,
                            )
                            first_eval = evaluate(
                                points, rows, atr, prediction_by_index, candidate,
                                fit_end, validation_1_end,
                                PRIMARY_FEE_BPS, PRIMARY_SLIPPAGE_BPS,
                            )
                            second_eval = evaluate(
                                points, rows, atr, prediction_by_index, candidate,
                                validation_1_end, validation_2_end,
                                PRIMARY_FEE_BPS, PRIMARY_SLIPPAGE_BPS,
                            )
                            evaluated += 1
                            first = first_eval.metrics
                            second = second_eval.metrics
                            if first.trades >= 15 and second.trades >= 15:
                                ranked.append(
                                    (
                                        rank_score(first, second),
                                        candidate,
                                        first_eval,
                                        second_eval,
                                        prediction_by_index,
                                        active_features,
                                    )
                                )
        print(f"Horizon {horizon}: evaluated {evaluated} candidates", flush=True)
    if not ranked:
        report = {
            "status": "NO_ELIGIBLE_CANDIDATE",
            "reason": "No flow model produced enough trades in both validations",
            "evaluated_candidates": evaluated,
            "coverage": {"start": iso(start_ts), "end": iso(final_ts)},
            "models": model_reports,
        }
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        return
    ranked.sort(key=lambda item: item[0], reverse=True)
    winner_score, winner, first_eval, second_eval, prediction_by_index, active_features = ranked[0]
    test_eval = evaluate(
        points, rows, atr, prediction_by_index, winner,
        validation_2_end, final_ts,
        PRIMARY_FEE_BPS, PRIMARY_SLIPPAGE_BPS,
    )
    stress = {}
    for label, fee, slippage in (
        ("RT6", 2.0, 1.0),
        ("RT8", 3.0, 1.0),
        ("RT10", 4.0, 1.0),
        ("RT12", 4.0, 2.0),
    ):
        stress[label] = evaluation_dict(evaluate(
            points, rows, atr, prediction_by_index, winner,
            fit_end, final_ts, fee, slippage,
        ))
    recent = {}
    for days in (7, 14, 30):
        recent[f"last_{days}d"] = evaluation_dict(evaluate(
            points, rows, atr, prediction_by_index, winner,
            max(validation_2_end, final_ts - days * 86400), final_ts,
            PRIMARY_FEE_BPS, PRIMARY_SLIPPAGE_BPS,
        ))
    first = first_eval.metrics
    second = second_eval.metrics
    test = test_eval.metrics
    validations_pass = (
        first.total_r > 0
        and second.total_r > 0
        and first.average_r > 0.03
        and second.average_r > 0.03
        and first.profit_factor >= 1.10
        and second.profit_factor >= 1.10
    )
    test_pass = (
        test.trades >= 15
        and test.total_r > 0
        and test.average_r > 0.04
        and test.profit_factor >= 1.15
    )
    stress_pass = (
        stress["RT10"]["total_r"] > 0
        and stress["RT10"]["profit_factor"] >= 1.05
    )
    status = (
        "CANDIDATE_FOR_FORWARD_PAPER"
        if validations_pass and test_pass and stress_pass
        else "NO_EDGE"
    )
    top = []
    for score, candidate, item_first, item_second, _, features in ranked[:12]:
        top.append(
            {
                "rank_score": round(score, 6),
                "candidate": asdict(candidate),
                "active_features": features,
                "validation_1": evaluation_dict(item_first),
                "validation_2": evaluation_dict(item_second),
            }
        )
    report = {
        "status": status,
        "method": (
            "Stable flow/derivatives correlation model with hourly source parity "
            "and causal per-exchange normalization; fitted on first 50% only"
        ),
        "selection_policy": (
            "Selected on two validations; final 20% is diagnostic because an "
            "earlier experiment already exposed that calendar period. Only new "
            "forward paper data can authorize live trading."
        ),
        "primary_cost": "8 bps round trip",
        "evaluated_candidates": evaluated,
        "eligible_candidates": len(ranked),
        "coverage": {
            "start": iso(start_ts),
            "fit_end": iso(fit_end),
            "validation_1_end": iso(validation_1_end),
            "validation_2_end": iso(validation_2_end),
            "latest_candle_end": iso(final_ts),
        },
        "winner": asdict(winner),
        "winner_active_features": active_features,
        "winner_rank_score": round(winner_score, 6),
        "validation_1": evaluation_dict(first_eval),
        "validation_2": evaluation_dict(second_eval),
        "untouched_latest_test": evaluation_dict(test_eval),
        "recent_performance_at_8bps": recent,
        "post_fit_cost_stress": stress,
        "top_validation_candidates": top,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"Report: {REPORT_PATH}", flush=True)


if __name__ == "__main__":
    main()
