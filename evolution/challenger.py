from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict

from learning.feature_weights import get_weights, normalize_weights


@dataclass
class ChallengerModel:
    symbol: str
    model_version: str
    status: str
    weights: Dict[str, float]
    parent_version: str
    reason: str

    def to_dict(self) -> Dict:
        return asdict(self)


def create_challenger(
    symbol: str = "BTC",
    parent_version: str = "1.0",
    challenger_version: str = "1.1",
    adjustments: Dict[str, float] | None = None,
    reason: str = "Experimental challenger",
) -> ChallengerModel:
    base_weights = get_weights(
        symbol=symbol,
        regime="GLOBAL",
        timeframe="MTF",
        model_version=parent_version,
    )

    candidate = base_weights.copy()

    if adjustments:
        for feature_name, delta in adjustments.items():
            if feature_name not in candidate:
                raise ValueError(
                    f"Невідомий feature: {feature_name}"
                )

            candidate[feature_name] = max(
                0.03,
                min(
                    0.45,
                    candidate[feature_name] + float(delta),
                ),
            )

    candidate = normalize_weights(candidate)

    return ChallengerModel(
        symbol=symbol,
        model_version=challenger_version,
        status="CHALLENGER",
        weights=candidate,
        parent_version=parent_version,
        reason=reason,
    )


if __name__ == "__main__":
    challenger = create_challenger(
        symbol="BTC",
        parent_version="1.0",
        challenger_version="1.1",
        adjustments={
            "orderflow": 0.02,
            "derivatives": -0.01,
            "structure": 0.01,
        },
        reason="Test stronger orderflow weighting",
    )

    print()
    print("ORACLE X — CHALLENGER MODEL")
    print("=" * 60)
    print(f"Symbol:   {challenger.symbol}")
    print(f"Version:  {challenger.model_version}")
    print(f"Parent:   {challenger.parent_version}")
    print(f"Status:   {challenger.status}")
    print(f"Reason:   {challenger.reason}")
    print()

    for feature, weight in sorted(challenger.weights.items()):
        print(f"{feature:<14} {weight:.4f}")
