from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Dict, Optional

from core.scoring import analyze_score, ScoringResult
from core.regime import analyze_regime
from database.db import connect


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


def get_model_config(
    model_version: str = "1.2",
) -> Dict:
    con = connect()

    try:
        row = con.execute(
            """
            SELECT model_version, status, config_json
            FROM model_registry
            WHERE model_version = ?
            """,
            (model_version,),
        ).fetchone()
    finally:
        con.close()

    if row is None:
        raise ValueError(
            f"Model {model_version} not found in model_registry"
        )

    config = json.loads(row["config_json"])

    config["_model_version"] = row["model_version"]
    config["_status"] = row["status"]

    return config


def reject(
    symbol: str,
    risk_percent: float,
    reason: str,
    direction: str = "NONE",
    entry: Optional[float] = None,
    stop: Optional[float] = None,
    tp: Optional[float] = None,
    rr: Optional[float] = None,
) -> RiskResult:
    return RiskResult(
        symbol=symbol,
        decision="REJECT",
        direction=direction,
        entry=round(entry, 2) if entry is not None else None,
        stop=round(stop, 2) if stop is not None else None,
        tp1=round(tp, 2) if tp is not None else None,
        tp2=round(tp, 2) if tp is not None else None,
        rr_tp1=round(rr, 2) if rr is not None else None,
        rr_tp2=round(rr, 2) if rr is not None else None,
        risk_percent=risk_percent,
        reason=reason,
    )


def analyze_risk(
    symbol: str = "BTC",
    risk_percent: float = 1.0,
    model_version: str = "1.2",
    scoring: Optional[ScoringResult] = None,
) -> RiskResult:
    config = get_model_config(model_version)

    min_score = float(
        config.get("min_score", 25.0)
    )
    min_agreement = float(
        config.get("min_agreement", 60.0)
    )
    stop_atr = float(
        config.get("stop_atr", 1.7)
    )
    tp_r = float(
        config.get("tp_r", 3.0)
    )

    if scoring is None:
        scoring = analyze_score(symbol)

    regime = analyze_regime(symbol)

    if scoring.bias == "NEUTRAL":
        return reject(
            symbol,
            risk_percent,
            "Немає достатньої directional переваги",
        )

    if abs(scoring.score) < min_score:
        return reject(
            symbol,
            risk_percent,
            f"Scoring нижче порогу моделі "
            f"{model_version}: "
            f"{abs(scoring.score):.2f} < {min_score:.2f}",
        )

    if scoring.agreement < min_agreement:
        return reject(
            symbol,
            risk_percent,
            f"Agreement нижче порогу моделі "
            f"{model_version}: "
            f"{scoring.agreement:.2f}% < "
            f"{min_agreement:.2f}%",
        )

    direction = (
        "LONG"
        if scoring.score > 0
        else "SHORT"
    )

    tf_15m = regime.timeframes["15m"]

    # IMPORTANT:
    # This is still current closed 15m close.
    # Next-candle-open execution parity will be added
    # in the paper/shadow execution layer.
    entry = float(tf_15m.close)
    atr_value = float(tf_15m.atr14)

    if atr_value <= 0:
        return reject(
            symbol,
            risk_percent,
            "ATR недоступний",
        )

    stop_distance = atr_value * stop_atr

    if direction == "LONG":
        stop = entry - stop_distance
        tp = entry + stop_distance * tp_r
    else:
        stop = entry + stop_distance
        tp = entry - stop_distance * tp_r

    rr = calculate_rr(
        entry,
        stop,
        tp,
        direction,
    )

    return RiskResult(
        symbol=symbol,
        decision="ALLOW",
        direction=direction,
        entry=round(entry, 2),
        stop=round(stop, 2),
        tp1=round(tp, 2),
        tp2=round(tp, 2),
        rr_tp1=round(rr, 2),
        rr_tp2=round(rr, 2),
        risk_percent=risk_percent,
        reason=(
            f"Risk-фільтри моделі {model_version} пройдено "
            f"(stop={stop_atr} ATR, TP={tp_r}R, "
            f"score>={min_score}, agreement>={min_agreement}%)"
        ),
    )


if __name__ == "__main__":
    result = analyze_risk(
        "BTC",
        model_version="1.2",
    )

    print()
    print("ORACLE X — MODEL-AWARE RISK ENGINE")
    print("=" * 60)
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
