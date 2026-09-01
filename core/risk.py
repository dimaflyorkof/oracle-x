from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, Optional

from core.scoring import analyze_score
from core.regime import analyze_regime


@dataclass
class RiskResult:
    symbol: str
    decision: str
    direction: str
    entry: Optional[float]
    stop: Optional[float]
    tp1: Optional[float]
    tp2: Optional[float]
    rr_tp1: Optional[float]
    rr_tp2: Optional[float]
    risk_percent: float
    reason: str

    def to_dict(self) -> Dict:
        return asdict(self)


def calculate_rr(
    entry: float,
    stop: float,
    target: float,
    direction: str,
) -> float:
    risk = abs(entry - stop)

    if risk <= 0:
        return 0.0

    if direction == "LONG":
        reward = target - entry
    else:
        reward = entry - target

    return reward / risk


def analyze_risk(
    symbol: str = "BTC",
    risk_percent: float = 1.0,
    min_score: float = 20.0,
    min_confidence: float = 35.0,
    min_rr: float = 1.5,
) -> RiskResult:
    scoring = analyze_score(symbol)
    regime = analyze_regime(symbol)

    if scoring.bias == "NEUTRAL":
        return RiskResult(
            symbol=symbol,
            decision="REJECT",
            direction="NONE",
            entry=None,
            stop=None,
            tp1=None,
            tp2=None,
            rr_tp1=None,
            rr_tp2=None,
            risk_percent=risk_percent,
            reason="Немає достатньої directional переваги",
        )

    if abs(scoring.score) < min_score:
        return RiskResult(
            symbol=symbol,
            decision="REJECT",
            direction="NONE",
            entry=None,
            stop=None,
            tp1=None,
            tp2=None,
            rr_tp1=None,
            rr_tp2=None,
            risk_percent=risk_percent,
            reason="Scoring нижче мінімального порогу",
        )

    if scoring.confidence < min_confidence:
        return RiskResult(
            symbol=symbol,
            decision="REJECT",
            direction="NONE",
            entry=None,
            stop=None,
            tp1=None,
            tp2=None,
            rr_tp1=None,
            rr_tp2=None,
            risk_percent=risk_percent,
            reason="Confidence нижче мінімального порогу",
        )

    direction = (
        "LONG"
        if scoring.bias == "BULLISH"
        else "SHORT"
    )

    tf_15m = regime.timeframes["15m"]

    entry = tf_15m.close
    atr_value = tf_15m.atr14

    if atr_value <= 0:
        return RiskResult(
            symbol=symbol,
            decision="REJECT",
            direction="NONE",
            entry=None,
            stop=None,
            tp1=None,
            tp2=None,
            rr_tp1=None,
            rr_tp2=None,
            risk_percent=risk_percent,
            reason="ATR недоступний",
        )

    stop_distance = atr_value * 1.5

    if regime.volatility == "HIGH":
        stop_distance *= 1.25

    elif regime.volatility == "LOW":
        stop_distance *= 0.85

    if direction == "LONG":
        stop = entry - stop_distance
        tp1 = entry + stop_distance * 1.5
        tp2 = entry + stop_distance * 2.5

    else:
        stop = entry + stop_distance
        tp1 = entry - stop_distance * 1.5
        tp2 = entry - stop_distance * 2.5

    rr_tp1 = calculate_rr(
        entry,
        stop,
        tp1,
        direction,
    )

    rr_tp2 = calculate_rr(
        entry,
        stop,
        tp2,
        direction,
    )

    if rr_tp1 < min_rr:
        return RiskResult(
            symbol=symbol,
            decision="REJECT",
            direction=direction,
            entry=round(entry, 2),
            stop=round(stop, 2),
            tp1=round(tp1, 2),
            tp2=round(tp2, 2),
            rr_tp1=round(rr_tp1, 2),
            rr_tp2=round(rr_tp2, 2),
            risk_percent=risk_percent,
            reason="Risk/Reward недостатній",
        )

    return RiskResult(
        symbol=symbol,
        decision="ALLOW",
        direction=direction,
        entry=round(entry, 2),
        stop=round(stop, 2),
        tp1=round(tp1, 2),
        tp2=round(tp2, 2),
        rr_tp1=round(rr_tp1, 2),
        rr_tp2=round(rr_tp2, 2),
        risk_percent=risk_percent,
        reason="Risk-фільтри пройдено",
    )


if __name__ == "__main__":
    result = analyze_risk("BTC")

    print()
    print("ORACLE X — RISK ENGINE")
    print("=" * 50)
    print(f"Decision:     {result.decision}")
    print(f"Direction:    {result.direction}")
    print(f"Entry:        {result.entry}")
    print(f"Stop:         {result.stop}")
    print(f"TP1:          {result.tp1}")
    print(f"TP2:          {result.tp2}")
    print(f"RR TP1:       {result.rr_tp1}")
    print(f"RR TP2:       {result.rr_tp2}")
    print(f"Risk:         {result.risk_percent}%")
    print(f"Reason:       {result.reason}")
