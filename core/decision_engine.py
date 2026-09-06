from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List

from core.regime import analyze_regime
from core.structure import analyze_structure
from core.momentum import analyze_momentum
from core.scoring import analyze_score
from core.risk import analyze_risk
from learning.historical_twins import analyze_historical_twins
from core.contradictions import analyze_contradictions
from core.data_freshness import analyze_freshness
from core.market_intelligence import analyze_market_intelligence


@dataclass
class DecisionResult:
    symbol: str
    decision: str
    confidence: float
    score: float
    entry: float | None
    stop: float | None
    tp1: float | None
    tp2: float | None
    rr_tp1: float | None
    rr_tp2: float | None
    reasons_for: List[str]
    reasons_against: List[str]
    warnings: List[str]
    data: Dict

    def to_dict(self) -> Dict:
        return asdict(self)


def build_reasons(
    symbol: str,
) -> tuple[List[str], List[str], List[str], Dict]:
    regime = analyze_regime(symbol)
    scoring = analyze_score(symbol)
    twins = analyze_historical_twins(
        symbol=symbol,
        timeframe="15m",
        top_n=10,
    )

    reasons_for: List[str] = []
    reasons_against: List[str] = []
    warnings: List[str] = []

    structure_data = {}
    momentum_data = {}

    for tf in ("15m", "1h", "4h"):
        structure = analyze_structure(symbol, tf)
        momentum = analyze_momentum(symbol, tf)

        structure_data[tf] = structure.to_dict()
        momentum_data[tf] = momentum.to_dict()

        if structure.structure == "BULLISH":
            reasons_for.append(
                f"{tf}: bullish market structure"
            )
        elif structure.structure == "BEARISH":
            reasons_against.append(
                f"{tf}: bearish market structure"
            )
        else:
            warnings.append(
                f"{tf}: structure is mixed/range"
            )

        if momentum.state == "BULLISH_MOMENTUM":
            reasons_for.append(
                f"{tf}: bullish momentum"
            )
        elif momentum.state == "BEARISH_MOMENTUM":
            reasons_against.append(
                f"{tf}: bearish momentum"
            )
        else:
            warnings.append(
                f"{tf}: momentum is neutral"
            )

        if momentum.acceleration == "FADING":
            warnings.append(
                f"{tf}: momentum is fading"
            )

        if momentum.divergence == "BULLISH_DIVERGENCE":
            reasons_for.append(
                f"{tf}: bullish RSI divergence"
            )

        if momentum.divergence == "BEARISH_DIVERGENCE":
            reasons_against.append(
                f"{tf}: bearish RSI divergence"
            )

        if structure.break_of_structure == "BULLISH_BOS":
            reasons_for.append(
                f"{tf}: bullish break of structure"
            )

        if structure.break_of_structure == "BEARISH_BOS":
            reasons_against.append(
                f"{tf}: bearish break of structure"
            )

        if structure.change_of_character == "BULLISH_CHOCH":
            reasons_for.append(
                f"{tf}: bullish change of character"
            )

        if structure.change_of_character == "BEARISH_CHOCH":
            reasons_against.append(
                f"{tf}: bearish change of character"
            )

    if regime.regime == "TREND_UP":
        reasons_for.append(
            "multi-timeframe regime is bullish"
        )
    elif regime.regime == "TREND_DOWN":
        reasons_against.append(
            "multi-timeframe regime is bearish"
        )
    else:
        warnings.append(
            "multi-timeframe regime is range/conflicted"
        )

    if regime.agreement < 60:
        warnings.append(
            f"low timeframe agreement: {regime.agreement:.1f}%"
        )

    if twins.historical_edge >= 15:
        if (
            twins.up_probability_4h is not None
            and twins.up_probability_4h >= 60
        ):
            reasons_for.append(
                f"historical twins: {twins.up_probability_4h:.1f}% "
                f"of similar cases rose over 4h"
            )

        elif (
            twins.up_probability_4h is not None
            and twins.up_probability_4h <= 40
        ):
            reasons_against.append(
                f"historical twins: only {twins.up_probability_4h:.1f}% "
                f"of similar cases rose over 4h"
            )
    else:
        warnings.append(
            f"historical edge is weak: {twins.historical_edge:.1f}%"
        )

    data = {
        "regime": regime.to_dict(),
        "scoring": scoring.to_dict(),
        "structure": structure_data,
        "momentum": momentum_data,
        "historical_twins": twins.to_dict(),
    }

    return reasons_for, reasons_against, warnings, data


def analyze_decision(
    symbol: str = "BTC",
) -> DecisionResult:
    freshness = analyze_freshness(symbol)

    if not freshness.is_fresh:
        return DecisionResult(
            symbol=symbol,
            decision="NO_TRADE",
            confidence=0.0,
            score=0.0,
            entry=None,
            stop=None,
            tp1=None,
            tp2=None,
            rr_tp1=None,
            rr_tp2=None,
            reasons_for=[],
            reasons_against=[],
            warnings=[
                "trade blocked by stale market data",
                "stale timeframes: " + ", ".join(
                    freshness.stale_timeframes
                ),
            ],
            data={
                "freshness": freshness.to_dict(),
            },
        )

    scoring = analyze_score(symbol)
    risk = analyze_risk(
        symbol,
        scoring=scoring,
    )
    contradictions = analyze_contradictions(symbol)

    intelligence = None
    intelligence_error = None

    try:
        intelligence = analyze_market_intelligence(symbol)
    except Exception as exc:
        intelligence_error = f"{type(exc).__name__}: {exc}"

    reasons_for, reasons_against, warnings, data = build_reasons(
        symbol
    )

    data["scoring"] = scoring.to_dict()
    data["risk"] = risk.to_dict()
    data["contradictions"] = contradictions.to_dict()

    if intelligence is not None:
        data["market_intelligence"] = asdict(intelligence)
    else:
        data["market_intelligence"] = {
            "status": "ERROR",
            "error": intelligence_error,
        }
        warnings.append(
            f"market intelligence unavailable: {intelligence_error}"
        )

    if risk.decision != "ALLOW":
        decision = "NO_TRADE"

    elif scoring.bias == "BULLISH":
        decision = "LONG"

    elif scoring.bias == "BEARISH":
        decision = "SHORT"

    else:
        decision = "NO_TRADE"

    # Market Intelligence is a context gate, not another entry signal.
    # It can allow the direction selected by price logic or block it.
    if intelligence is None:
        if decision in ("LONG", "SHORT"):
            decision = "NO_TRADE"
            warnings.append(
                "trade blocked: market intelligence failed closed"
            )

    elif intelligence.data_coverage < 0.65:
        if decision in ("LONG", "SHORT"):
            decision = "NO_TRADE"
        warnings.append(
            "trade blocked: market intelligence coverage "
            f"{intelligence.data_coverage:.1%} < 65.0%"
        )

    elif decision == "LONG":
        if intelligence.decision != "LONG_ALLOWED":
            decision = "NO_TRADE"
            warnings.append(
                "LONG blocked by market intelligence: "
                f"{intelligence.decision}, "
                f"score={intelligence.score:.3f}"
            )
        else:
            reasons_for.append(
                "market intelligence allows LONG: "
                f"state={intelligence.market_state}, "
                f"flow={intelligence.flow_state}, "
                f"score={intelligence.score:.3f}"
            )

    elif decision == "SHORT":
        if intelligence.decision != "SHORT_ALLOWED":
            decision = "NO_TRADE"
            warnings.append(
                "SHORT blocked by market intelligence: "
                f"{intelligence.decision}, "
                f"score={intelligence.score:.3f}"
            )
        else:
            reasons_against.append(
                "market intelligence allows SHORT: "
                f"state={intelligence.market_state}, "
                f"flow={intelligence.flow_state}, "
                f"score={intelligence.score:.3f}"
            )

    elif intelligence.decision == "BLOCK":
        warnings.append(
            "market intelligence context is neutral/conflicted: "
            f"score={intelligence.score:.3f}"
        )

    confidence = scoring.confidence

    if intelligence is not None:
        confidence *= (
            0.70
            + 0.30 * intelligence.confidence
        )

    if contradictions.severity == "MEDIUM":
        confidence *= 0.85
        warnings.append(
            f"contradiction severity MEDIUM: score={contradictions.score}"
        )

    elif contradictions.severity == "HIGH":
        confidence *= 0.60
        warnings.append(
            f"contradiction severity HIGH: score={contradictions.score}"
        )

        if decision in ("LONG", "SHORT"):
            decision = "NO_TRADE"
            warnings.append(
                "trade blocked by high contradiction risk"
            )

    elif contradictions.severity == "LOW":
        warnings.append(
            f"contradiction severity LOW: score={contradictions.score}"
        )

    for item in contradictions.contradictions:
        warnings.append(
            f"conflict: {item}"
        )

    if (
        decision == "NO_TRADE"
        and risk.decision != "ALLOW"
    ):
        warnings.append(
            f"trade rejected: {risk.reason}"
        )
    elif decision == "NO_TRADE":
        warnings.append(
            "trade rejected by decision/context filters"
        )

    if decision == "LONG":
        if len(reasons_against) > len(reasons_for):
            warnings.append(
                "contradictory bearish evidence remains"
            )

    if decision == "SHORT":
        if len(reasons_for) > len(reasons_against):
            warnings.append(
                "contradictory bullish evidence remains"
            )

    return DecisionResult(
        symbol=symbol,
        decision=decision,
        confidence=round(confidence, 2),
        score=round(scoring.score, 2),
        entry=risk.entry,
        stop=risk.stop,
        tp1=risk.tp1,
        tp2=risk.tp2,
        rr_tp1=risk.rr_tp1,
        rr_tp2=risk.rr_tp2,
        reasons_for=reasons_for,
        reasons_against=reasons_against,
        warnings=warnings,
        data=data,
    )


if __name__ == "__main__":
    result = analyze_decision("BTC")

    print()
    print("ORACLE X — DECISION ENGINE")
    print("=" * 60)
    print(f"Symbol:      {result.symbol}")
    print(f"Decision:    {result.decision}")
    print(f"Score:       {result.score}")
    print(f"Confidence:  {result.confidence}%")
    print()

    print(f"Entry:       {result.entry}")
    print(f"Stop:        {result.stop}")
    print(f"TP1:         {result.tp1}")
    print(f"TP2:         {result.tp2}")
    print(f"RR TP1:      {result.rr_tp1}")
    print(f"RR TP2:      {result.rr_tp2}")
    print()

    print("REASONS FOR:")
    if result.reasons_for:
        for reason in result.reasons_for:
            print(f"  + {reason}")
    else:
        print("  none")

    print()

    print("REASONS AGAINST:")
    if result.reasons_against:
        for reason in result.reasons_against:
            print(f"  - {reason}")
    else:
        print("  none")

    print()

    print("WARNINGS:")
    if result.warnings:
        for warning in result.warnings:
            print(f"  ! {warning}")
    else:
        print("  none")
