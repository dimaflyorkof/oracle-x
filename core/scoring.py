from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict

from core.regime import analyze_regime
from core.structure import analyze_structure
from core.momentum import analyze_momentum
from learning.feature_weights import get_weights
from database.db import connect


TIMEFRAME_WEIGHTS = {
    "15m": 0.20,
    "1h": 0.35,
    "4h": 0.45,
}


@dataclass
class ScoringResult:
    symbol: str
    score: float
    confidence: float
    bias: str
    regime_score: float
    structure_score: float
    momentum_score: float
    agreement: float
    details: Dict

    def to_dict(self) -> Dict:
        return asdict(self)


def structure_to_score(structure: str) -> float:
    if structure == "BULLISH":
        return 1.0
    if structure == "BEARISH":
        return -1.0
    return 0.0


def momentum_to_score(state: str) -> float:
    if state == "BULLISH_MOMENTUM":
        return 1.0
    if state == "BEARISH_MOMENTUM":
        return -1.0
    return 0.0


def regime_to_score(regime: str) -> float:
    if regime == "TREND_UP":
        return 1.0
    if regime == "TREND_DOWN":
        return -1.0
    return 0.0


def latest_live_scores(symbol: str = "BTC") -> Dict[str, float]:
    con = connect()

    try:
        orderflow = con.execute(
            """
            SELECT imbalance, delta
            FROM orderflow_history
            WHERE symbol = ?
            ORDER BY timestamp_unix DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()

        derivatives = con.execute(
            """
            SELECT long_short_ratio, taker_ratio
            FROM derivatives_history
            WHERE symbol = ?
            ORDER BY timestamp_unix DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()

        liquidations = con.execute(
            """
            SELECT
                COALESCE(SUM(long_liquidations), 0) AS long_total,
                COALESCE(SUM(short_liquidations), 0) AS short_total
            FROM (
                SELECT long_liquidations, short_liquidations
                FROM liquidation_history
                WHERE symbol = ?
                ORDER BY timestamp_unix DESC
                LIMIT 20
            )
            """,
            (symbol,),
        ).fetchone()

    finally:
        con.close()

    orderflow_score = 0.0
    derivatives_score = 0.0
    liquidation_score = 0.0

    if orderflow is not None:
        imbalance = float(orderflow["imbalance"] or 0.0)
        delta = float(orderflow["delta"] or 0.0)

        if imbalance >= 0.20 and delta > 0:
            orderflow_score = 1.0
        elif imbalance <= -0.20 and delta < 0:
            orderflow_score = -1.0
        elif delta > 0:
            orderflow_score = 0.5
        elif delta < 0:
            orderflow_score = -0.5

    if derivatives is not None:
        ls_ratio = float(derivatives["long_short_ratio"] or 0.0)
        taker_ratio = float(derivatives["taker_ratio"] or 0.0)

        if taker_ratio >= 1.10:
            derivatives_score += 0.6
        elif taker_ratio <= 0.90:
            derivatives_score -= 0.6

        # Crowding is treated cautiously:
        # extreme long/short imbalance is not a direct directional signal.
        if 1.05 <= ls_ratio < 1.20:
            derivatives_score += 0.2
        elif 0.80 < ls_ratio <= 0.95:
            derivatives_score -= 0.2

        derivatives_score = max(
            -1.0,
            min(1.0, derivatives_score),
        )

    if liquidations is not None:
        long_total = float(liquidations["long_total"] or 0.0)
        short_total = float(liquidations["short_total"] or 0.0)

        if short_total > long_total * 2 and short_total > 0:
            liquidation_score = 0.5
        elif long_total > short_total * 2 and long_total > 0:
            liquidation_score = -0.5

    return {
        "orderflow": orderflow_score,
        "derivatives": derivatives_score,
        "liquidations": liquidation_score,
    }


def analyze_score(symbol: str = "BTC") -> ScoringResult:
    regime = analyze_regime(symbol)

    structure_results = {}
    momentum_results = {}

    weighted_structure = 0.0
    weighted_momentum = 0.0

    bullish_weight = 0.0
    bearish_weight = 0.0
    neutral_weight = 0.0

    for tf, weight in TIMEFRAME_WEIGHTS.items():
        structure = analyze_structure(symbol, tf)
        momentum = analyze_momentum(symbol, tf)

        structure_results[tf] = structure
        momentum_results[tf] = momentum

        structure_component = structure_to_score(
            structure.structure
        ) * (structure.confidence / 100.0)

        momentum_component = momentum_to_score(
            momentum.state
        ) * (momentum.confidence / 100.0)

        weighted_structure += structure_component * weight
        weighted_momentum += momentum_component * weight

        combined_tf = structure_component + momentum_component

        if combined_tf > 0.25:
            bullish_weight += weight
        elif combined_tf < -0.25:
            bearish_weight += weight
        else:
            neutral_weight += weight

    regime_component = regime_to_score(regime.regime) * (
        regime.confidence / 100.0
    )

    weights = get_weights(symbol)

    regime_weight = weights.get("regime", 0.40)
    structure_weight = weights.get("structure", 0.35)
    momentum_weight = weights.get("momentum", 0.25)

    raw_score = (
        regime_component * regime_weight
        + weighted_structure * structure_weight
        + weighted_momentum * momentum_weight
    )

    live_scores = latest_live_scores(symbol)

    # Live layer is intentionally capped.
    # It adjusts the core score but cannot dominate it.
    live_adjustment = (
        live_scores["orderflow"] * 0.12
        + live_scores["derivatives"] * 0.08
        + live_scores["liquidations"] * 0.05
    )

    raw_score += live_adjustment

    score = raw_score * 100.0

    dominant_weight = max(
        bullish_weight,
        bearish_weight,
        neutral_weight,
    )

    agreement = dominant_weight * 100.0

    confidence = min(
        100.0,
        abs(score) * 0.70 + agreement * 0.30,
    )

    if score >= 20:
        bias = "BULLISH"
    elif score <= -20:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    return ScoringResult(
        symbol=symbol,
        score=round(score, 2),
        confidence=round(confidence, 2),
        bias=bias,
        regime_score=round(regime_component * 100.0, 2),
        structure_score=round(weighted_structure * 100.0, 2),
        momentum_score=round(weighted_momentum * 100.0, 2),
        agreement=round(agreement, 2),
        details={
            "live_scores": live_scores,
            "live_adjustment": round(live_adjustment * 100.0, 2),
            "regime": regime.to_dict(),
            "structure": {
                tf: result.to_dict()
                for tf, result in structure_results.items()
            },
            "momentum": {
                tf: result.to_dict()
                for tf, result in momentum_results.items()
            },
        },
    )


if __name__ == "__main__":
    result = analyze_score("BTC")

    print()
    print("ORACLE X — CORE SCORING")
    print("=" * 50)
    print(f"Symbol:          {result.symbol}")
    print(f"Bias:            {result.bias}")
    print(f"Score:           {result.score}")
    print(f"Confidence:      {result.confidence}%")
    print(f"Agreement:       {result.agreement}%")
    print()
    print(f"Regime score:    {result.regime_score}")
    print(f"Structure score: {result.structure_score}")
    print(f"Momentum score:  {result.momentum_score}")
