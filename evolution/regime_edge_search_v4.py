from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from evolution.backtest import load_rows, simulate_trade
from evolution.flow_edge_search_v3 import (
    FEATURE_NAMES,
    FeaturePoint,
    build_feature_points,
    correlation_weights,
    mean,
    quantile,
    standardized,
    standardizer,
    trade_metrics,
)


SYMBOL = "BTC"
REPORT_PATH = Path("regime_edge_search_v4_result.json")
SELECTION_FEE_BPS = 4.0
SELECTION_SLIPPAGE_BPS = 1.0  # 10 bps round trip
STRESS_FEE_BPS = 4.0
STRESS_SLIPPAGE_BPS = 2.0  # 12 bps round trip

ATR_PERCENT = FEATURE_NAMES.index("atr_percent")
RETURN_96 = FEATURE_NAMES.index("return_96")
SPOT_FLOW_1H = FEATURE_NAMES.index("spot_flow_1h")
FUTURES_FLOW_1H = FEATURE_NAMES.index("futures_flow_1h")
FUNDING_Z = FEATURE_NAMES.index("funding_causal_z")
CROWDING_Z = FEATURE_NAMES.index("crowding_causal_z")
DERIVATIVES_COVERAGE = FEATURE_NAMES.index("derivatives_coverage")


@dataclass(frozen=True)
class Candidate:
    horizon: int
    top_k: int
    signal_quantile: float
    stop_atr: float
    tp_r: float
    mode: str
    cooldown_bars: int = 1


@dataclass
class RegimeModel:
    centers: List[float]
    scales: List[float]
    weights: List[float]
    threshold: float


@dataclass
class FoldResult:
    fold: int
    start_ts: int
    end_ts: int
    trades: list
    signals: int
    vetoed: int
    models: Dict[str, dict]


def iso(ts: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def causal_regimes(points: Sequence[FeaturePoint]) -> Dict[int, str]:
    """Classify each point using only values available before that point."""
    result: Dict[int, str] = {}
    atr_history: List[float] = []
    window = 96 * 14

    for point in points:
        atr_percent = max(float(point.values[ATR_PERCENT]), 1e-12)
        return_96 = float(point.values[RETURN_96])
        history = atr_history[-window:]

        if len(history) < 96 * 2:
            result[point.index] = "WARMUP"
        else:
            baseline = max(float(median(history)), 1e-12)
            volatility_ratio = atr_percent / baseline
            trend_strength = abs(return_96) / (
                atr_percent * math.sqrt(96.0)
            )

            if volatility_ratio >= 1.35:
                regime = "HIGH_VOL"
            elif trend_strength >= 1.15:
                regime = "TREND_UP" if return_96 > 0 else "TREND_DOWN"
            else:
                regime = "RANGE"
            result[point.index] = regime

        atr_history.append(atr_percent)

    return result


def allowed_regime(mode: str, regime: str) -> bool:
    if mode == "TREND_ONLY":
        return regime in {"TREND_UP", "TREND_DOWN"}
    if mode == "RANGE_ONLY":
        return regime == "RANGE"
    if mode == "ADAPTIVE":
        return regime in {"TREND_UP", "TREND_DOWN", "RANGE"}
    raise ValueError(f"Unknown mode: {mode}")


def fit_models(
    rows: list,
    atr: Sequence[Optional[float]],
    train_points: Sequence[FeaturePoint],
    regimes: Dict[int, str],
    candidate: Candidate,
) -> Dict[str, RegimeModel]:
    result: Dict[str, RegimeModel] = {}

    for regime in ("TREND_UP", "TREND_DOWN", "RANGE"):
        subset = [
            point
            for point in train_points
            if regimes.get(point.index) == regime
        ]
        if len(subset) < 250:
            continue

        centers, scales = standardizer(subset)
        weights = correlation_weights(
            rows,
            atr,
            subset,
            centers,
            scales,
            candidate.horizon,
            candidate.top_k,
        )
        if not any(weights):
            continue

        scores = [
            abs(sum(
                value * weight
                for value, weight in zip(
                    standardized(point, centers, scales),
                    weights,
                )
            ))
            for point in subset
        ]
        threshold = quantile(scores, candidate.signal_quantile)
        result[regime] = RegimeModel(
            centers=centers,
            scales=scales,
            weights=weights,
            threshold=threshold,
        )

    return result


def direction_for(
    point: FeaturePoint,
    regime: str,
    model: RegimeModel,
) -> Optional[str]:
    z = standardized(point, model.centers, model.scales)
    score = sum(value * weight for value, weight in zip(z, model.weights))
    if abs(score) < model.threshold:
        return None

    direction = "LONG" if score > 0 else "SHORT"

    # Trend models cannot fight their causally identified price regime.
    if regime == "TREND_UP" and direction != "LONG":
        return None
    if regime == "TREND_DOWN" and direction != "SHORT":
        return None
    return direction


def risk_veto(point: FeaturePoint, direction: str) -> bool:
    """Use derivatives only as a causal veto, never as a sole entry trigger."""
    coverage = float(point.values[DERIVATIVES_COVERAGE])
    if coverage < 0.5:
        return False

    funding = float(point.values[FUNDING_Z])
    crowding = float(point.values[CROWDING_Z])
    if direction == "LONG" and funding > 2.0 and crowding > 2.0:
        return True
    if direction == "SHORT" and funding < -2.0 and crowding < -2.0:
        return True
    return False


def flow_confirms(point: FeaturePoint, direction: str) -> bool:
    """Reject only when both independent flow proxies oppose the trade."""
    spot = float(point.values[SPOT_FLOW_1H])
    futures = float(point.values[FUTURES_FLOW_1H])
    if direction == "LONG":
        return not (spot < -0.05 and futures < -0.05)
    return not (spot > 0.05 and futures > 0.05)


def evaluate_fold(
    rows: list,
    atr: Sequence[Optional[float]],
    points: Sequence[FeaturePoint],
    regimes: Dict[int, str],
    models: Dict[str, RegimeModel],
    candidate: Candidate,
    fold: int,
    start_ts: int,
    end_ts: int,
    fee_bps: float,
    slippage_bps: float,
) -> FoldResult:
    trades = []
    signals = 0
    vetoed = 0
    next_allowed_index = 0

    for point in points:
        if point.timestamp_unix < start_ts:
            continue
        if point.timestamp_unix >= end_ts:
            break
        if point.index < next_allowed_index:
            continue

        regime = regimes.get(point.index, "WARMUP")
        if not allowed_regime(candidate.mode, regime):
            continue
        model = models.get(regime)
        if model is None:
            continue

        direction = direction_for(point, regime, model)
        if direction is None:
            continue
        signals += 1

        if risk_veto(point, direction) or not flow_confirms(point, direction):
            vetoed += 1
            continue

        atr_value = atr[point.index]
        if atr_value is None or atr_value <= 0:
            continue
        trade = simulate_trade(
            rows,
            point.index,
            direction,
            float(atr_value),
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

    model_report = {}
    for regime, model in models.items():
        model_report[regime] = {
            "threshold": round(model.threshold, 6),
            "features": [
                {
                    "name": FEATURE_NAMES[index],
                    "weight": round(weight, 6),
                }
                for index, weight in enumerate(model.weights)
                if weight != 0.0
            ],
        }

    return FoldResult(
        fold=fold,
        start_ts=start_ts,
        end_ts=end_ts,
        trades=trades,
        signals=signals,
        vetoed=vetoed,
        models=model_report,
    )


def fold_dict(value: FoldResult) -> dict:
    result = asdict(trade_metrics(value.trades))
    result.update({
        "fold": value.fold,
        "start": iso(value.start_ts),
        "end": iso(value.end_ts),
        "signals": value.signals,
        "vetoed": value.vetoed,
        "models": value.models,
    })
    return result


def combined_metrics(folds: Iterable[FoldResult]) -> dict:
    trades = [trade for fold in folds for trade in fold.trades]
    return asdict(trade_metrics(trades))


def fold_boundaries(points: Sequence[FeaturePoint]) -> List[Tuple[int, int]]:
    timestamps = [point.timestamp_unix for point in points]
    boundaries = [timestamps[int(len(timestamps) * value)] for value in (0.50, 0.625, 0.75, 0.875)]
    final_ts = timestamps[-1] + 900
    ends = boundaries[1:] + [final_ts]
    return list(zip(boundaries, ends))


def run_candidate(
    rows: list,
    atr: Sequence[Optional[float]],
    points: Sequence[FeaturePoint],
    regimes: Dict[int, str],
    candidate: Candidate,
    fee_bps: float,
    slippage_bps: float,
) -> List[FoldResult]:
    results = []
    for fold, (start_ts, end_ts) in enumerate(fold_boundaries(points), 1):
        embargo_ts = start_ts - (candidate.horizon + 1) * 900
        train_points = [
            point
            for point in points
            if point.timestamp_unix < embargo_ts
        ]
        models = fit_models(rows, atr, train_points, regimes, candidate)
        results.append(evaluate_fold(
            rows,
            atr,
            points,
            regimes,
            models,
            candidate,
            fold,
            start_ts,
            end_ts,
            fee_bps,
            slippage_bps,
        ))
    return results


def score_candidate(folds: Sequence[FoldResult]) -> float:
    metrics = [trade_metrics(fold.trades) for fold in folds]
    if any(item.trades < 8 for item in metrics):
        return -999.0
    worst_avg = min(item.average_r for item in metrics)
    worst_pf = min(item.profit_factor for item in metrics)
    dispersion = max(item.average_r for item in metrics) - worst_avg
    drawdown = max(item.max_drawdown_r for item in metrics)
    return (
        worst_avg
        + 0.10 * (worst_pf - 1.0)
        - 0.25 * dispersion
        - 0.01 * drawdown
    )


def main() -> None:
    print("ORACLE X — REGIME EDGE SEARCH V4", flush=True)
    print("Expanding walk-forward | causal regimes | selection at 10 bps", flush=True)
    print("Previously exposed history is diagnostic only", flush=True)

    rows = load_rows(SYMBOL, "15m")
    points, atr = build_feature_points(rows)
    if len(points) < 5000:
        raise RuntimeError(f"Not enough complete feature points: {len(points)}")
    regimes = causal_regimes(points)
    regime_counts = {
        regime: sum(value == regime for value in regimes.values())
        for regime in ("TREND_UP", "TREND_DOWN", "RANGE", "HIGH_VOL", "WARMUP")
    }
    print(f"Complete points: {len(points)}", flush=True)
    print(f"Regimes: {regime_counts}", flush=True)

    candidates = [
        Candidate(horizon, top_k, signal_quantile, stop_atr, tp_r, mode)
        for horizon in (8, 16, 32)
        for top_k in (4, 6)
        for signal_quantile in (0.90, 0.95)
        for stop_atr in (2.0, 2.5)
        for tp_r in (2.0, 3.0)
        for mode in ("TREND_ONLY", "RANGE_ONLY", "ADAPTIVE")
    ]
    print(f"Bounded hypotheses: {len(candidates)}", flush=True)

    ranked = []
    for index, candidate in enumerate(candidates, 1):
        folds = run_candidate(
            rows,
            atr,
            points,
            regimes,
            candidate,
            SELECTION_FEE_BPS,
            SELECTION_SLIPPAGE_BPS,
        )
        ranked.append((score_candidate(folds), candidate, folds))
        if index % 24 == 0:
            print(f"Evaluated {index}/{len(candidates)}", flush=True)

    ranked.sort(key=lambda item: item[0], reverse=True)
    winner_score, winner, winner_folds = ranked[0]
    stress_folds = run_candidate(
        rows,
        atr,
        points,
        regimes,
        winner,
        STRESS_FEE_BPS,
        STRESS_SLIPPAGE_BPS,
    )
    fold_metrics = [trade_metrics(fold.trades) for fold in winner_folds]
    combined = combined_metrics(winner_folds)
    stress = combined_metrics(stress_folds)

    historical_gate = (
        combined["trades"] >= 50
        and combined["average_r"] >= 0.08
        and combined["profit_factor"] >= 1.20
        and combined["max_drawdown_r"] <= 12.0
        and all(item.total_r > 0 for item in fold_metrics)
        and all(item.profit_factor >= 1.05 for item in fold_metrics)
        and stress["total_r"] > 0
        and stress["profit_factor"] >= 1.10
    )
    status = "HISTORICAL_CANDIDATE_ONLY" if historical_gate else "NO_EDGE"

    report = {
        "status": status,
        "warning": (
            "All historical periods have been exposed. This run cannot authorize "
            "live trading; only new forward paper observations can do so."
        ),
        "method": (
            "Causal regime classification, separate expanding-window models, "
            "embargoed folds, derivatives veto, dual-source flow confirmation"
        ),
        "selection_cost": "10 bps round trip",
        "stress_cost": "12 bps round trip",
        "evaluated_hypotheses": len(candidates),
        "coverage": {
            "start": iso(points[0].timestamp_unix),
            "end": iso(points[-1].timestamp_unix + 900),
        },
        "regime_counts": regime_counts,
        "winner": asdict(winner),
        "winner_score": round(winner_score, 6),
        "walk_forward_folds": [fold_dict(fold) for fold in winner_folds],
        "combined_at_10bps": combined,
        "stress_at_12bps": stress,
        "promotion_gate": {
            "historical_gate_passed": historical_gate,
            "minimum_trades": 50,
            "minimum_average_r": 0.08,
            "minimum_profit_factor": 1.20,
            "maximum_drawdown_r": 12.0,
            "every_fold_positive": True,
            "stress_pf_minimum": 1.10,
        },
        "top_candidates": [
            {
                "score": round(score, 6),
                "candidate": asdict(candidate),
                "combined": combined_metrics(folds),
                "folds": [fold_dict(fold) for fold in folds],
            }
            for score, candidate, folds in ranked[:10]
        ],
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print(f"Report: {REPORT_PATH}", flush=True)


if __name__ == "__main__":
    main()
