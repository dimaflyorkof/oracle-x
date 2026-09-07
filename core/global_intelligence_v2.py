from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Optional

from core.market_intelligence import (
    Component,
    clamp,
    component,
    price_change,
    robust_z,
    score_liquidations,
    score_macro,
    score_onchain,
    score_sentiment,
)
from database.db import connect


SYMBOL = "BTC"
VERSION = "GLOBAL-INTELLIGENCE-V2"

SPOT_SOURCES = (
    ("binance", "binance_spot_kline_15m", False, 0.40),
    ("coinbase", "coinbase_spot_trades_15m", True, 0.30),
    ("kraken", "kraken_spot_trades_15m", True, 0.30),
)
FUTURES_SOURCE = "binance_futures_kline_15m"
DERIVATIVE_SOURCES = (
    "binance_futures",
    "bybit_futures",
    "okx_futures",
)

COMPONENT_WEIGHTS = {
    "global_spot": 0.35,
    "futures_flow": 0.15,
    "derivatives": 0.20,
    "macro": 0.15,
    "onchain": 0.05,
    "sentiment": 0.05,
    "liquidations": 0.05,
}


@dataclass
class GlobalIntelligenceV2Result:
    version: str
    symbol: str
    as_of_unix: int
    decision: str
    market_state: str
    flow_state: str
    score: float
    confidence: float
    data_coverage: float
    spot_consensus: str
    contradictions: List[str]
    components: Dict[str, Dict[str, Any]]
    unavailable: Dict[str, str]
    policy: Dict[str, Any]


def weighted_average(values: Iterable[tuple[float, float]]) -> float:
    items = [(float(value), float(weight)) for value, weight in values if weight > 0]
    denominator = sum(weight for _, weight in items)
    if denominator <= 1e-12:
        return 0.0
    return sum(value * weight for value, weight in items) / denominator


def flow_imbalance(rows: List[Dict[str, Any]], length: int) -> float:
    selected = rows[-min(length, len(rows)):]
    buy = sum(float(row.get("buy_volume") or 0.0) for row in selected)
    sell = sum(float(row.get("sell_volume") or 0.0) for row in selected)
    total = buy + sell
    return (buy - sell) / total if total > 0 else 0.0


def flow_score(rows: List[Dict[str, Any]]) -> tuple[float, Dict[str, float]]:
    windows = {
        "imbalance_1h": flow_imbalance(rows, 4),
        "imbalance_4h": flow_imbalance(rows, 16),
        "imbalance_24h": flow_imbalance(rows, 96),
    }
    score = (
        0.50 * math.tanh(windows["imbalance_1h"] / 0.08)
        + 0.30 * math.tanh(windows["imbalance_4h"] / 0.10)
        + 0.20 * math.tanh(windows["imbalance_24h"] / 0.12)
    )
    return clamp(score), {key: round(value, 6) for key, value in windows.items()}


def complete_global_row(row: Dict[str, Any]) -> bool:
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(
        raw.get("quality") == "COMPLETE"
        and raw.get("finalized") is True
        and raw.get("connection_interrupted") is False
    )


def load_flow_rows(
    source: str,
    as_of_ts: int,
    quality_required: bool,
    limit: int = 96,
) -> List[Dict[str, Any]]:
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT timestamp_unix, buy_volume, sell_volume, raw_json
            FROM orderflow_history
            WHERE symbol = ?
              AND source = ?
              AND timestamp_unix + 900 <= ?
              AND buy_volume IS NOT NULL
              AND sell_volume IS NOT NULL
            ORDER BY timestamp_unix DESC, id DESC
            LIMIT ?
            """,
            (SYMBOL, source, int(as_of_ts), int(limit * 2 if quality_required else limit)),
        ).fetchall()
    finally:
        con.close()
    result = []
    seen = set()
    for raw_row in rows:
        row = dict(raw_row)
        timestamp_unix = int(row["timestamp_unix"])
        if timestamp_unix in seen:
            continue
        if quality_required and not complete_global_row(row):
            continue
        seen.add(timestamp_unix)
        result.append(row)
        if len(result) >= limit:
            break
    return list(reversed(result))


def source_flow_result(
    label: str,
    source: str,
    quality_required: bool,
    as_of_ts: int,
) -> Dict[str, Any]:
    rows = load_flow_rows(source, as_of_ts, quality_required)
    latest = int(rows[-1]["timestamp_unix"]) + 900 if rows else None
    age = max(0, as_of_ts - latest) if latest is not None else None
    if len(rows) < 4:
        return {
            "label": label,
            "source": source,
            "status": "ACCUMULATING" if rows else "NO_COMPLETE_DATA",
            "active": False,
            "score": 0.0,
            "confidence": 0.0,
            "rows": len(rows),
            "latest_available_unix": latest,
            "age_seconds": age,
        }
    score, windows = flow_score(rows)
    sample_confidence = min(1.0, len(rows) / 16.0)
    freshness = 1.0 if age is not None and age <= 1800 else 0.60 if age is not None and age <= 7200 else 0.0
    confidence = sample_confidence * freshness
    return {
        "label": label,
        "source": source,
        "status": "ACTIVE" if confidence > 0 else "STALE",
        "active": confidence > 0,
        "score": round(score, 6),
        "confidence": round(confidence, 6),
        "rows": len(rows),
        "latest_available_unix": latest,
        "age_seconds": age,
        **windows,
    }


def score_global_spot(as_of_ts: int) -> Component:
    sources = {
        label: source_flow_result(label, source, quality, as_of_ts)
        for label, source, quality, _ in SPOT_SOURCES
    }
    configured_weights = {label: weight for label, _, _, weight in SPOT_SOURCES}
    active = [item for item in sources.values() if item["active"]]
    latest = max(
        (item["latest_available_unix"] for item in active if item["latest_available_unix"] is not None),
        default=None,
    )
    observations = sum(int(item["rows"]) for item in sources.values())
    if not active:
        return component(
            "global_spot", "ACCUMULATING", False, 0.0, 0.0,
            observations, latest, as_of_ts, {"sources": sources, "consensus": "COLLECT"},
        )
    score = weighted_average(
        (
            item["score"],
            configured_weights[item["label"]] * item["confidence"],
        )
        for item in active
    )
    directional = [item for item in active if abs(float(item["score"])) >= 0.12]
    bullish = sum(float(item["score"]) > 0 for item in directional)
    bearish = sum(float(item["score"]) < 0 for item in directional)
    if len(active) < 2:
        consensus = "SINGLE_SOURCE"
    elif bullish >= 2:
        consensus = "BUY"
    elif bearish >= 2:
        consensus = "SELL"
    else:
        consensus = "MIXED"
    agreement = max(bullish, bearish) / len(directional) if directional else 0.5
    breadth = sum(configured_weights[item["label"]] for item in active)
    sample_quality = weighted_average(
        (
            item["confidence"],
            configured_weights[item["label"]],
        )
        for item in active
    )
    confidence = breadth * sample_quality * (0.55 + 0.45 * agreement)
    globally_active = len(active) >= 2
    status = "ACTIVE" if globally_active else "PARTIAL"
    return component(
        "global_spot", status, globally_active, score, confidence,
        observations, latest, as_of_ts,
        {
            "consensus": consensus,
            "active_exchanges": len(active),
            "bullish_votes": bullish,
            "bearish_votes": bearish,
            "sources": sources,
        },
    )


def score_futures_flow(as_of_ts: int) -> Component:
    result = source_flow_result(
        "binance_futures",
        FUTURES_SOURCE,
        False,
        as_of_ts,
    )
    return component(
        "futures_flow",
        result["status"],
        bool(result["active"]),
        float(result["score"]),
        float(result["confidence"]),
        int(result["rows"]),
        result["latest_available_unix"],
        as_of_ts,
        result,
    )


def table_columns(table: str) -> set[str]:
    con = connect()
    try:
        return {str(row["name"]) for row in con.execute(f"PRAGMA table_info({table})")}
    finally:
        con.close()


def score_derivatives_causal(as_of_ts: int) -> Component:
    columns = table_columns("derivatives_history")
    availability = (
        "COALESCE(available_at_unix, timestamp_unix)"
        if "available_at_unix" in columns
        else "timestamp_unix"
    )
    event_time = (
        "COALESCE(event_timestamp_unix, timestamp_unix)"
        if "event_timestamp_unix" in columns
        else "timestamp_unix"
    )
    price_move = price_change(as_of_ts, 4)
    source_results: Dict[str, Dict[str, Any]] = {}
    latest_available = None
    observations = 0
    con = connect()
    try:
        for source in DERIVATIVE_SOURCES:
            rows = con.execute(
                f"""
                SELECT {event_time} AS event_unix,
                       {availability} AS available_unix,
                       funding_rate, open_interest_change,
                       long_short_ratio, taker_ratio, futures_basis
                FROM derivatives_history
                WHERE symbol = ? AND source = ?
                  AND {availability} <= ?
                ORDER BY {availability} DESC, id DESC
                LIMIT 240
                """,
                (SYMBOL, source, int(as_of_ts)),
            ).fetchall()
            if len(rows) < 20:
                continue
            rows = list(reversed(rows))
            observations += len(rows)
            source_latest = int(rows[-1]["available_unix"])
            latest_available = max(latest_available or 0, source_latest)
            signals: Dict[str, float] = {}
            funding = [row["funding_rate"] for row in rows if row["funding_rate"] is not None]
            if len(funding) >= 8:
                signals["funding_contrarian"] = -clamp(robust_z(funding) / 2.0)
            oi = [row["open_interest_change"] for row in rows if row["open_interest_change"] is not None]
            if len(oi) >= 8 and abs(price_move) > 1e-9:
                intensity = abs(clamp(robust_z(oi) / 2.0))
                signals["oi_with_price"] = intensity if price_move > 0 else -intensity
            taker = [row["taker_ratio"] for row in rows if row["taker_ratio"] is not None]
            if len(taker) >= 8:
                signals["taker_flow"] = clamp(robust_z(taker) / 2.0)
            crowding = [row["long_short_ratio"] for row in rows if row["long_short_ratio"] is not None]
            if len(crowding) >= 8:
                signals["crowding_contrarian"] = -clamp(robust_z(crowding) / 2.0)
            basis = [row["futures_basis"] for row in rows if row["futures_basis"] is not None]
            if len(basis) >= 8:
                signals["basis"] = clamp(robust_z(basis) / 3.0)
            if not signals:
                continue
            age = max(0, as_of_ts - source_latest)
            freshness = 1.0 if age <= 7200 else 0.60 if age <= 86400 else 0.0
            source_results[source] = {
                "score": round(statistics.fmean(signals.values()), 6),
                "confidence": round(freshness, 6),
                "rows": len(rows),
                "latest_available_unix": source_latest,
                "signals": {key: round(value, 6) for key, value in signals.items()},
            }
    finally:
        con.close()
    active = [item for item in source_results.values() if item["confidence"] > 0]
    if not active:
        return component(
            "derivatives", "INSUFFICIENT_HISTORY", False, 0.0, 0.0,
            observations, latest_available, as_of_ts,
            {"price_change_4h": round(price_move, 6), "sources": source_results},
        )
    score = weighted_average((item["score"], item["confidence"]) for item in active)
    directional = [item for item in active if abs(float(item["score"])) >= 0.10]
    positive = sum(float(item["score"]) > 0 for item in directional)
    negative = sum(float(item["score"]) < 0 for item in directional)
    agreement = max(positive, negative) / len(directional) if directional else 0.5
    confidence = min(1.0, len(active) / len(DERIVATIVE_SOURCES)) * (0.55 + 0.45 * agreement)
    return component(
        "derivatives", "ACTIVE", True, score, confidence,
        observations, latest_available, as_of_ts,
        {"price_change_4h": round(price_move, 6), "sources": source_results},
    )


def analyze_global_intelligence_v2(
    symbol: str = SYMBOL,
    as_of_ts: Optional[int] = None,
) -> GlobalIntelligenceV2Result:
    if symbol != SYMBOL:
        raise ValueError("Global Intelligence V2 currently supports BTC only")
    as_of = int(as_of_ts if as_of_ts is not None else time.time())
    items = {
        "global_spot": score_global_spot(as_of),
        "futures_flow": score_futures_flow(as_of),
        "derivatives": score_derivatives_causal(as_of),
        "macro": score_macro(as_of),
        "onchain": score_onchain(as_of),
        "sentiment": score_sentiment(as_of),
        "liquidations": score_liquidations(as_of),
    }
    weighted_confidence = [
        (item.score, COMPONENT_WEIGHTS[name] * item.confidence)
        for name, item in items.items()
        if item.active and item.confidence > 0
    ]
    score = weighted_average(weighted_confidence)
    active_weight = sum(
        COMPONENT_WEIGHTS[name]
        for name, item in items.items()
        if item.active
    )
    coverage = active_weight / sum(COMPONENT_WEIGHTS.values())
    spot_consensus = str(items["global_spot"].details.get("consensus", "COLLECT"))
    contradictions = []
    bullish = [name for name, item in items.items() if item.active and item.score >= 0.25]
    bearish = [name for name, item in items.items() if item.active and item.score <= -0.25]
    if bullish and bearish:
        contradictions.append(
            "Bullish: " + ", ".join(bullish) + "; bearish: " + ", ".join(bearish)
        )
    if items["global_spot"].active and items["futures_flow"].active:
        if items["global_spot"].score * items["futures_flow"].score < -0.04:
            contradictions.append("Spot and futures flow disagree")

    decision = "BLOCK"
    market_state = "NEUTRAL"
    global_spot_ready = (
        items["global_spot"].active
        and items["global_spot"].confidence >= 0.35
    )
    if global_spot_ready and coverage >= 0.55 and score >= 0.20:
        decision, market_state = "LONG_ALLOWED", "RISK_ON"
    elif global_spot_ready and coverage >= 0.55 and score <= -0.20:
        decision, market_state = "SHORT_ALLOWED", "RISK_OFF"
    if spot_consensus == "MIXED" and abs(score) < 0.35:
        decision = "BLOCK"
    if contradictions and abs(score) < 0.30:
        decision = "BLOCK"

    flow_score = weighted_average(
        (
            (items["global_spot"].score, 0.70 * items["global_spot"].confidence),
            (items["futures_flow"].score, 0.30 * items["futures_flow"].confidence),
        )
    )
    if flow_score >= 0.18:
        flow_state = "ACCUMULATION"
    elif flow_score <= -0.18:
        flow_state = "DISTRIBUTION"
    else:
        flow_state = "MIXED"

    confidence = clamp(abs(score) * 0.55 + coverage * 0.30 + items["global_spot"].confidence * 0.15)
    unavailable = {name: item.status for name, item in items.items() if not item.active}
    return GlobalIntelligenceV2Result(
        version=VERSION,
        symbol=symbol,
        as_of_unix=as_of,
        decision=decision,
        market_state=market_state,
        flow_state=flow_state,
        score=round(score, 6),
        confidence=round(confidence, 6),
        data_coverage=round(coverage, 6),
        spot_consensus=spot_consensus,
        contradictions=contradictions,
        components={name: asdict(item) for name, item in items.items()},
        unavailable=unavailable,
        policy={
            "deployment": "SHADOW_ONLY",
            "missing_data": "INACTIVE_NOT_ZERO",
            "global_spot_minimum": "TWO_ACTIVE_EXCHANGES",
            "global_spot_minimum_confidence": 0.35,
            "derivatives_visibility": "AVAILABLE_AT_OR_CONSERVATIVE_TIMESTAMP",
            "decision_threshold": 0.20,
            "minimum_coverage": 0.55,
        },
    )


def main() -> None:
    result = analyze_global_intelligence_v2()
    print("ORACLE X — GLOBAL INTELLIGENCE V2", flush=True)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
