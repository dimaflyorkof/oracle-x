from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict

from core.regime import analyze_regime
from core.structure import analyze_structure
from core.momentum import analyze_momentum


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

    # Initial engine weights.
    # Later these values will be learned dynamically.
    regime_weight = 0.40
    structure_weight = 0.35
    momentum_weight = 0.25

    raw_score = (
        regime_component * regime_weight
        + weighted_structure * structure_weight
        + weighted_momentum * momentum_weight
    )

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
