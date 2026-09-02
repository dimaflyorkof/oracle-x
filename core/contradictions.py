from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from database.db import connect
from core.regime import analyze_regime
from core.structure import analyze_structure
from core.momentum import analyze_momentum


@dataclass
class ContradictionResult:
    symbol: str
    score: float
    severity: str
    contradictions: List[str]
    confirmations: List[str]
    warnings: List[str]
    data: Dict

    def to_dict(self) -> Dict:
        return asdict(self)


def latest_row(table: str, symbol: str):
    con = connect()

    try:
        row = con.execute(
            f"""
            SELECT *
            FROM {table}
            WHERE symbol = ?
            ORDER BY timestamp_unix DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
    finally:
        con.close()

    return row


def recent_liquidations(
    symbol: str,
    limit: int = 20,
) -> Dict[str, float]:
    con = connect()

    try:
        rows = con.execute(
            """
            SELECT long_liquidations, short_liquidations
            FROM liquidation_history
            WHERE symbol = ?
            ORDER BY timestamp_unix DESC
            LIMIT ?
            """,
            (symbol, limit),
        ).fetchall()

    finally:
        con.close()

    long_total = sum(
        float(row["long_liquidations"] or 0.0)
        for row in rows
    )

    short_total = sum(
        float(row["short_liquidations"] or 0.0)
        for row in rows
    )

    return {
        "long": long_total,
        "short": short_total,
        "total": long_total + short_total,
    }


def analyze_contradictions(
    symbol: str = "BTC",
) -> ContradictionResult:
    contradictions: List[str] = []
    confirmations: List[str] = []
    warnings: List[str] = []

    score = 0.0

    orderflow = latest_row(
        "orderflow_history",
        symbol,
    )

    derivatives = latest_row(
        "derivatives_history",
        symbol,
    )

    liquidations = recent_liquidations(
        symbol,
        limit=20,
    )

    regime = analyze_regime(symbol)

    structures = {
        tf: analyze_structure(symbol, tf)
        for tf in ("15m", "1h", "4h")
    }

    momentums = {
        tf: analyze_momentum(symbol, tf)
        for tf in ("15m", "1h", "4h")
    }

    orderflow_direction: Optional[str] = None
    derivatives_direction: Optional[str] = None

    if orderflow is not None:
        imbalance = float(
            orderflow["imbalance"] or 0.0
        )

        delta = float(
            orderflow["delta"] or 0.0
        )

        if imbalance >= 0.20 and delta > 0:
            orderflow_direction = "BULLISH"
            confirmations.append(
                "orderflow: покупці домінують"
            )

        elif imbalance <= -0.20 and delta < 0:
            orderflow_direction = "BEARISH"
            confirmations.append(
                "orderflow: продавці домінують"
            )

        else:
            orderflow_direction = "MIXED"
            warnings.append(
                "orderflow: немає чіткої переваги"
            )

    else:
        warnings.append(
            "orderflow: немає даних"
        )

    if derivatives is not None:
        ls_ratio = float(
            derivatives["long_short_ratio"] or 0.0
        )

        taker_ratio = float(
            derivatives["taker_ratio"] or 0.0
        )

        bullish_points = 0
        bearish_points = 0

        if taker_ratio >= 1.10:
            bullish_points += 1

        elif taker_ratio <= 0.90:
            bearish_points += 1

        if ls_ratio >= 1.20:
            bullish_points += 1
            warnings.append(
                f"LONG/SHORT={ls_ratio:.2f}: лонгів багато"
            )

        elif ls_ratio <= 0.80:
            bearish_points += 1
            warnings.append(
                f"LONG/SHORT={ls_ratio:.2f}: шортів багато"
            )

        if bullish_points > bearish_points:
            derivatives_direction = "BULLISH"
            confirmations.append(
                "derivatives: перевага bullish"
            )

        elif bearish_points > bullish_points:
            derivatives_direction = "BEARISH"
            confirmations.append(
                "derivatives: перевага bearish"
            )

        else:
            derivatives_direction = "MIXED"

    else:
        warnings.append(
            "derivatives: немає даних"
        )

    if (
        orderflow_direction == "BEARISH"
        and derivatives_direction == "BULLISH"
    ):
        contradictions.append(
            "ведмежий orderflow суперечить bullish derivatives"
        )
        score += 2.0

    if (
        orderflow_direction == "BULLISH"
        and derivatives_direction == "BEARISH"
    ):
        contradictions.append(
            "bullish orderflow суперечить bearish derivatives"
        )
        score += 2.0

    long_liq = liquidations["long"]
    short_liq = liquidations["short"]

    if long_liq > short_liq * 2 and long_liq > 0:
        warnings.append(
            "домінують ліквідації LONG"
        )

        if orderflow_direction == "BEARISH":
            confirmations.append(
                "ліквідації LONG підтверджують тиск продавців"
            )

        elif orderflow_direction == "BULLISH":
            contradictions.append(
                "bullish orderflow при домінуванні ліквідацій LONG"
            )
            score += 1.0

    elif short_liq > long_liq * 2 and short_liq > 0:
        warnings.append(
            "домінують ліквідації SHORT"
        )

        if orderflow_direction == "BULLISH":
            confirmations.append(
                "ліквідації SHORT підтверджують тиск покупців"
            )

        elif orderflow_direction == "BEARISH":
            contradictions.append(
                "bearish orderflow при домінуванні ліквідацій SHORT"
            )
            score += 1.0

    tf_directions = []

    for tf in ("15m", "1h", "4h"):
        structure = structures[tf]
        momentum = momentums[tf]

        structure_dir = (
            "BULLISH"
            if structure.structure == "BULLISH"
            else "BEARISH"
            if structure.structure == "BEARISH"
            else "NEUTRAL"
        )

        momentum_dir = (
            "BULLISH"
            if momentum.state == "BULLISH_MOMENTUM"
            else "BEARISH"
            if momentum.state == "BEARISH_MOMENTUM"
            else "NEUTRAL"
        )

        tf_directions.append(
            (tf, structure_dir, momentum_dir)
        )

        if (
            structure_dir != "NEUTRAL"
            and momentum_dir != "NEUTRAL"
            and structure_dir != momentum_dir
        ):
            contradictions.append(
                f"{tf}: structure={structure_dir}, "
                f"momentum={momentum_dir}"
            )
            score += 1.0

    if regime.agreement < 60:
        contradictions.append(
            f"низька MTF узгодженість: {regime.agreement:.1f}%"
        )
        score += 1.0

    if score >= 4:
        severity = "HIGH"
    elif score >= 2:
        severity = "MEDIUM"
    elif score > 0:
        severity = "LOW"
    else:
        severity = "NONE"

    return ContradictionResult(
        symbol=symbol,
        score=round(score, 2),
        severity=severity,
        contradictions=contradictions,
        confirmations=confirmations,
        warnings=warnings,
        data={
            "orderflow_direction": orderflow_direction,
            "derivatives_direction": derivatives_direction,
            "liquidations": liquidations,
            "regime": regime.to_dict(),
            "structures": {
                tf: value.to_dict()
                for tf, value in structures.items()
            },
            "momentums": {
                tf: value.to_dict()
                for tf, value in momentums.items()
            },
        },
    )


if __name__ == "__main__":
    result = analyze_contradictions("BTC")

    print()
    print("ORACLE X — CONTRADICTION ENGINE")
    print("=" * 60)
    print(f"Symbol:   {result.symbol}")
    print(f"Score:    {result.score}")
    print(f"Severity: {result.severity}")
    print()

    print("CONTRADICTIONS:")
    if result.contradictions:
        for item in result.contradictions:
            print(f"  ! {item}")
    else:
        print("  none")

    print()

    print("CONFIRMATIONS:")
    if result.confirmations:
        for item in result.confirmations:
            print(f"  + {item}")
    else:
        print("  none")

    print()

    print("WARNINGS:")
    if result.warnings:
        for item in result.warnings:
            print(f"  - {item}")
    else:
        print("  none")
