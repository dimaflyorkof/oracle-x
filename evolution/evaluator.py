from dataclasses import dataclass, asdict
from typing import Dict, List

from evolution.model_registry import get_model


@dataclass
class EvaluationResult:
    symbol: str
    champion_version: str
    challenger_version: str
    decision: str
    score: float
    passed_checks: int
    total_checks: int
    reasons: List[str]
    warnings: List[str]
    metrics: Dict


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0

    return ((new - old) / abs(old)) * 100.0


def evaluate_models(
    symbol: str,
    champion_version: str,
    challenger_version: str,
) -> EvaluationResult:
    champion = get_model(symbol, champion_version)
    challenger = get_model(symbol, challenger_version)

    if champion is None:
        raise ValueError(
            f"Champion not found: {symbol} {champion_version}"
        )

    if challenger is None:
        raise ValueError(
            f"Challenger not found: {symbol} {challenger_version}"
        )

    c = champion["metrics"]
    n = challenger["metrics"]

    required = (
        "trades",
        "win_rate",
        "total_r",
        "average_r",
        "profit_factor",
        "max_drawdown_r",
    )

    for metric in required:
        if metric not in c:
            raise ValueError(
                f"Champion missing metric: {metric}"
            )

        if metric not in n:
            raise ValueError(
                f"Challenger missing metric: {metric}"
            )

    reasons = []
    warnings = []

    checks = []

    # -----------------------------------------------------
    # 1. Profit Factor
    # Challenger should improve PF by at least 5%
    # and remain above a minimum absolute quality level.
    # -----------------------------------------------------

    pf_improvement = pct_change(
        n["profit_factor"],
        c["profit_factor"],
    )

    pf_pass = (
        n["profit_factor"] >= 1.20
        and pf_improvement >= 5.0
    )

    checks.append(pf_pass)

    if pf_pass:
        reasons.append(
            f"Profit Factor improved by {pf_improvement:.2f}% "
            f"({c['profit_factor']:.4f} -> "
            f"{n['profit_factor']:.4f})"
        )
    else:
        warnings.append(
            f"Profit Factor improvement insufficient: "
            f"{pf_improvement:.2f}%"
        )

    # -----------------------------------------------------
    # 2. Average R / expectancy
    # -----------------------------------------------------

    expectancy_improvement = pct_change(
        n["average_r"],
        c["average_r"],
    )

    expectancy_pass = (
        n["average_r"] > 0
        and expectancy_improvement >= 5.0
    )

    checks.append(expectancy_pass)

    if expectancy_pass:
        reasons.append(
            f"Average R improved by "
            f"{expectancy_improvement:.2f}% "
            f"({c['average_r']:.4f} -> "
            f"{n['average_r']:.4f})"
        )
    else:
        warnings.append(
            f"Average R improvement insufficient: "
            f"{expectancy_improvement:.2f}%"
        )

    # -----------------------------------------------------
    # 3. Drawdown
    # Challenger must not materially worsen DD.
    # Passing if equal/better, or no more than 5% worse.
    # -----------------------------------------------------

    champion_dd = float(c["max_drawdown_r"])
    challenger_dd = float(n["max_drawdown_r"])

    dd_limit = champion_dd * 1.05

    dd_pass = challenger_dd <= dd_limit

    checks.append(dd_pass)

    if dd_pass:
        reasons.append(
            f"Drawdown acceptable "
            f"({champion_dd:.2f}R -> "
            f"{challenger_dd:.2f}R)"
        )
    else:
        warnings.append(
            f"Drawdown worsened beyond tolerance "
            f"({champion_dd:.2f}R -> "
            f"{challenger_dd:.2f}R)"
        )

    # -----------------------------------------------------
    # 4. Sample size
    # Avoid candidates that look good only because
    # they produced too few trades.
    # -----------------------------------------------------

    minimum_trades = max(
        200,
        int(c["trades"] * 0.70),
    )

    sample_pass = n["trades"] >= minimum_trades

    checks.append(sample_pass)

    if sample_pass:
        reasons.append(
            f"Sample size acceptable: "
            f"{n['trades']} trades"
        )
    else:
        warnings.append(
            f"Sample size too small: "
            f"{n['trades']} < {minimum_trades}"
        )

    # -----------------------------------------------------
    # 5. Win rate
    # Not mandatory to be huge, but challenger should
    # not collapse relative to Champion.
    # -----------------------------------------------------

    win_rate_pass = (
        n["win_rate"] >= c["win_rate"] - 3.0
    )

    checks.append(win_rate_pass)

    if win_rate_pass:
        reasons.append(
            f"Win rate stable/improved "
            f"({c['win_rate']:.2f}% -> "
            f"{n['win_rate']:.2f}%)"
        )
    else:
        warnings.append(
            f"Win rate deteriorated materially "
            f"({c['win_rate']:.2f}% -> "
            f"{n['win_rate']:.2f}%)"
        )

    # -----------------------------------------------------
    # 6. Total R
    # Supporting metric only.
    # -----------------------------------------------------

    total_r_pass = n["total_r"] > c["total_r"]

    checks.append(total_r_pass)

    if total_r_pass:
        reasons.append(
            f"Total R improved "
            f"({c['total_r']:.2f}R -> "
            f"{n['total_r']:.2f}R)"
        )
    else:
        warnings.append(
            f"Total R did not improve "
            f"({c['total_r']:.2f}R -> "
            f"{n['total_r']:.2f}R)"
        )

    passed = sum(bool(x) for x in checks)
    total = len(checks)

    score = passed / total * 100.0

    # Hard safety failures.
    hard_fail = (
        n["average_r"] <= 0
        or n["profit_factor"] < 1.0
        or challenger_dd > champion_dd * 1.50
        or n["trades"] < 100
    )

    if hard_fail:
        decision = "FAIL"
    elif passed == total:
        decision = "PASS"
    elif score >= 66.0:
        decision = "REVIEW"
    else:
        decision = "FAIL"

    return EvaluationResult(
        symbol=symbol,
        champion_version=champion_version,
        challenger_version=challenger_version,
        decision=decision,
        score=round(score, 2),
        passed_checks=passed,
        total_checks=total,
        reasons=reasons,
        warnings=warnings,
        metrics={
            "champion": c,
            "challenger": n,
            "profit_factor_improvement_pct": round(
                pf_improvement, 2
            ),
            "average_r_improvement_pct": round(
                expectancy_improvement, 2
            ),
        },
    )


def print_evaluation(result: EvaluationResult) -> None:
    print()
    print("ORACLE X — MODEL EVALUATION")
    print("=" * 70)

    print(f"Symbol:       {result.symbol}")
    print(
        f"Champion:     v{result.champion_version}"
    )
    print(
        f"Challenger:   v{result.challenger_version}"
    )

    print(f"Decision:     {result.decision}")
    print(
        f"Score:        {result.score:.2f}% "
        f"({result.passed_checks}/{result.total_checks})"
    )

    print()
    print("REASONS")
    print("-" * 70)

    for reason in result.reasons:
        print(f"+ {reason}")

    if result.warnings:
        print()
        print("WARNINGS")
        print("-" * 70)

        for warning in result.warnings:
            print(f"! {warning}")


if __name__ == "__main__":
    result = evaluate_models(
        symbol="BTC",
        champion_version="1.0",
        challenger_version="1.1",
    )

    print_evaluation(result)
