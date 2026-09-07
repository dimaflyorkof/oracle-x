from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from core.global_intelligence_v2 import (
    COMPONENT_WEIGHTS,
    GlobalIntelligenceV2Result,
    analyze_global_intelligence_v2,
    clamp,
)
from database.db import connect


SYMBOL = "BTC"
VERSION = "GLOBAL-INTELLIGENCE-V2.1"
SOURCE = "alfred_vintages"
DECISION_THRESHOLD = 0.20
MINIMUM_COVERAGE = 0.55
MACRO_WEIGHT = 0.20

# Fixed before forward observation. Positive means risk-on for BTC.
SERIES = {
    "BAMLH0A0HYM2": ("US_HIGH_YIELD_OAS", -1.0, "DAILY", 10),
    "DFF": ("FED_FUNDS_EFFECTIVE_RATE", -1.0, "DAILY", 10),
    "DGS10": ("US_TREASURY_10Y", -1.0, "DAILY", 10),
    "DTWEXBGS": ("US_DOLLAR_BROAD_INDEX", -1.0, "DAILY", 10),
    "NFCI": ("CHICAGO_FED_NFCI", -1.0, "WEEKLY", 8),
    "VIXCLS": ("VIX_CLOSE", -1.0, "DAILY", 10),
    "CPIAUCSL": ("US_CPI_ALL", -1.0, "MONTHLY", 12),
    "CPILFESL": ("US_CPI_CORE", -1.0, "MONTHLY", 12),
    "PAYEMS": ("US_NONFARM_PAYROLLS", 1.0, "MONTHLY", 12),
    "UNRATE": ("US_UNEMPLOYMENT_RATE", -1.0, "MONTHLY", 12),
}

FRESHNESS_SECONDS = {
    "DAILY": 7 * 86400,
    "WEEKLY": 21 * 86400,
    "MONTHLY": 70 * 86400,
}


@dataclass
class MacroVintageComponent:
    name: str
    status: str
    active: bool
    score: float
    confidence: float
    observations: int
    active_series: int
    latest_available_unix: Optional[int]
    age_seconds: Optional[int]
    details: Dict[str, Any]


@dataclass
class GlobalIntelligenceV21Result:
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
    macro_vintage: Dict[str, Any]
    base_v2: Dict[str, Any]
    policy: Dict[str, Any]


def _median(values: List[float]) -> float:
    return statistics.median(values)


def _robust_z(values: List[float]) -> float:
    if len(values) < 8:
        return 0.0
    center = _median(values)
    deviations = [abs(value - center) for value in values]
    mad = _median(deviations)
    if mad <= 1e-12:
        return 0.0
    return (values[-1] - center) / (1.4826 * mad)


def _transform(values: List[float], frequency: str) -> List[float]:
    if frequency == "MONTHLY":
        if len(values) < 2:
            return []
        return [
            values[index] - values[index - 1]
            for index in range(1, len(values))
        ]
    if frequency == "WEEKLY":
        return [
            values[index] - values[index - 1]
            for index in range(1, len(values))
        ]
    return [
        values[index] - values[index - 5]
        for index in range(5, len(values))
    ]


def _load_visible_series(series_id: str, as_of_ts: int, limit: int = 800) -> List[Dict[str, Any]]:
    con = connect()
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='global_macro_vintages'"
        ).fetchone()
        if not exists:
            return []
        rows = con.execute(
            """
            SELECT id, series_id, series_name, observation_date,
                   observation_timestamp_unix, realtime_start,
                   available_at_unix, value, quality
            FROM global_macro_vintages
            WHERE source = ? AND series_id = ?
              AND available_at_unix <= ?
              AND available_at_unix >= observation_timestamp_unix
            ORDER BY observation_timestamp_unix DESC, available_at_unix DESC, id DESC
            LIMIT ?
            """,
            (SOURCE, series_id, int(as_of_ts), int(limit)),
        ).fetchall()
    finally:
        con.close()
    # One initial-release value per observation date; no later revision is consulted.
    unique: Dict[int, Dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        observation = int(item["observation_timestamp_unix"])
        if observation not in unique:
            unique[observation] = item
    return [unique[key] for key in sorted(unique)]


def score_macro_vintages(as_of_ts: int) -> MacroVintageComponent:
    series_details: Dict[str, Any] = {}
    contributions: List[tuple[float, float]] = []
    observations = 0
    latest: Optional[int] = None
    for series_id, (name, direction, frequency, minimum) in SERIES.items():
        rows = _load_visible_series(series_id, as_of_ts)
        observations += len(rows)
        if not rows:
            series_details[series_id] = {"name": name, "status": "NO_VISIBLE_DATA"}
            continue
        available = int(rows[-1]["available_at_unix"])
        latest = max(latest or 0, available)
        age = max(0, int(as_of_ts) - available)
        values = [float(row["value"]) for row in rows]
        changes = _transform(values, frequency)
        if len(changes) < minimum:
            series_details[series_id] = {
                "name": name,
                "status": "INSUFFICIENT_HISTORY",
                "rows": len(rows),
                "latest_available_unix": available,
            }
            continue
        freshness_limit = FRESHNESS_SECONDS[frequency]
        freshness = 1.0 if age <= freshness_limit else 0.5 if age <= freshness_limit * 2 else 0.0
        if freshness <= 0:
            series_details[series_id] = {
                "name": name,
                "status": "STALE",
                "rows": len(rows),
                "age_seconds": age,
            }
            continue
        raw_z = _robust_z(changes)
        signal = clamp(direction * math.tanh(raw_z / 2.5))
        contributions.append((signal, freshness))
        series_details[series_id] = {
            "name": name,
            "status": "ACTIVE",
            "frequency": frequency,
            "rows": len(rows),
            "latest_value": round(values[-1], 6),
            "latest_change": round(changes[-1], 6),
            "causal_z": round(raw_z, 6),
            "risk_score": round(signal, 6),
            "latest_available_unix": available,
            "age_seconds": age,
        }
    active = len(contributions)
    if not active:
        return MacroVintageComponent(
            "macro_vintage", "INSUFFICIENT_VISIBLE_DATA", False, 0.0, 0.0,
            observations, 0, latest, None if latest is None else max(0, as_of_ts - latest),
            {"series": series_details},
        )
    score = sum(value * weight for value, weight in contributions) / sum(weight for _, weight in contributions)
    signs = [1 if value > 0.05 else -1 if value < -0.05 else 0 for value, _ in contributions]
    directional = [value for value in signs if value]
    agreement = max(directional.count(1), directional.count(-1)) / len(directional) if directional else 0.5
    coverage = active / len(SERIES)
    confidence = clamp(coverage * (0.60 + 0.40 * agreement))
    return MacroVintageComponent(
        "macro_vintage", "ACTIVE", True, round(score, 6), round(confidence, 6),
        observations, active, latest, None if latest is None else max(0, as_of_ts - latest),
        {"series": series_details, "fixed_weight": MACRO_WEIGHT},
    )


def analyze_global_intelligence_v2_1(
    symbol: str = SYMBOL,
    as_of_ts: Optional[int] = None,
) -> GlobalIntelligenceV21Result:
    if symbol != SYMBOL:
        raise ValueError("Global Intelligence V2.1 currently supports BTC only")
    as_of = int(as_of_ts if as_of_ts is not None else time.time())
    base: GlobalIntelligenceV2Result = analyze_global_intelligence_v2(symbol, as_of_ts=as_of)
    macro = score_macro_vintages(as_of)
    # Replace V2's legacy macro component. Remaining V2 components are rescaled to
    # 80%; ALFRED receives 20%. Missing macro stays inactive and weights renormalize.
    non_macro_total = sum(
        weight for name, weight in COMPONENT_WEIGHTS.items() if name != "macro"
    )
    weighted_components: List[tuple[float, float]] = []
    active_nominal_weight = 0.0
    confidence_numerator = 0.0
    for name, item in base.components.items():
        if name == "macro" or not item.get("active"):
            continue
        nominal = COMPONENT_WEIGHTS[name] / non_macro_total * (1.0 - MACRO_WEIGHT)
        component_confidence = float(item.get("confidence") or 0.0)
        if component_confidence <= 0:
            continue
        weighted_components.append((float(item.get("score") or 0.0), nominal * component_confidence))
        active_nominal_weight += nominal
        confidence_numerator += nominal * component_confidence
    if macro.active:
        weighted_components.append((macro.score, MACRO_WEIGHT * macro.confidence))
        active_nominal_weight += MACRO_WEIGHT
        confidence_numerator += MACRO_WEIGHT * macro.confidence
    denominator = sum(weight for _, weight in weighted_components)
    score = (
        sum(value * weight for value, weight in weighted_components) / denominator
        if denominator > 1e-12 else 0.0
    )
    coverage = clamp(active_nominal_weight)
    confidence = clamp(confidence_numerator / active_nominal_weight) if active_nominal_weight else 0.0
    effective_macro_weight = (
        MACRO_WEIGHT * macro.confidence / denominator
        if macro.active and denominator > 1e-12 else 0.0
    )

    contradictions = list(base.contradictions)
    if macro.active and base.score * macro.score < -0.04:
        contradictions.append("Official vintage macro and market intelligence disagree")

    decision, market_state = "BLOCK", "NEUTRAL"
    global_spot = base.components.get("global_spot", {})
    spot_ready = bool(global_spot.get("active")) and float(global_spot.get("confidence") or 0.0) >= 0.35
    if spot_ready and coverage >= MINIMUM_COVERAGE and score >= DECISION_THRESHOLD:
        decision, market_state = "LONG_ALLOWED", "RISK_ON"
    elif spot_ready and coverage >= MINIMUM_COVERAGE and score <= -DECISION_THRESHOLD:
        decision, market_state = "SHORT_ALLOWED", "RISK_OFF"
    if base.spot_consensus == "MIXED" and abs(score) < 0.35:
        decision = "BLOCK"
    if contradictions and abs(score) < 0.30:
        decision = "BLOCK"

    return GlobalIntelligenceV21Result(
        version=VERSION,
        symbol=symbol,
        as_of_unix=as_of,
        decision=decision,
        market_state=market_state,
        flow_state=base.flow_state,
        score=round(score, 6),
        confidence=round(confidence, 6),
        data_coverage=round(coverage, 6),
        spot_consensus=base.spot_consensus,
        contradictions=contradictions,
        macro_vintage=asdict(macro),
        base_v2={
            "decision": base.decision,
            "score": base.score,
            "confidence": base.confidence,
            "data_coverage": base.data_coverage,
        },
        policy={
            "deployment": "SHADOW_ONLY",
            "trading_authority": False,
            "historical_backtest_authority": False,
            "macro_source": SOURCE,
            "macro_visibility": "AVAILABLE_AT_UNIX_LE_AS_OF",
            "vintage_kind": "INITIAL_RELEASE",
            "missing_data": "INACTIVE_NOT_ZERO",
            "fixed_macro_weight": MACRO_WEIGHT,
            "effective_macro_weight": round(effective_macro_weight, 6),
            "decision_threshold": DECISION_THRESHOLD,
            "minimum_coverage": MINIMUM_COVERAGE,
        },
    )


def main() -> None:
    print("ORACLE X — GLOBAL INTELLIGENCE V2.1", flush=True)
    print(json.dumps(asdict(analyze_global_intelligence_v2_1()), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
