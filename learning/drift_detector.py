from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List

from database.db import connect


@dataclass
class PerformanceWindow:
    sample_size: int
    wins: int
    losses: int
    win_rate: float
    average_r: float

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class DriftResult:
    symbol: str
    state: str
    confidence_multiplier: float
    recent: PerformanceWindow
    baseline: PerformanceWindow
    win_rate_change: float
    average_r_change: float
    reason: str

    def to_dict(self) -> Dict:
        return asdict(self)


def calculate_window(rows: List) -> PerformanceWindow:
    if not rows:
        return PerformanceWindow(
            sample_size=0,
            wins=0,
            losses=0,
            win_rate=0.0,
            average_r=0.0,
        )

    wins = 0
    losses = 0
    total_r = 0.0

    for row in rows:
        value = float(row["result_r"] or 0.0)
        total_r += value

        if value > 0:
            wins += 1
        elif value < 0:
            losses += 1

    sample_size = len(rows)

    return PerformanceWindow(
        sample_size=sample_size,
        wins=wins,
        losses=losses,
        win_rate=round(wins / sample_size, 4),
        average_r=round(total_r / sample_size, 4),
    )


def analyze_drift(
    symbol: str = "BTC",
    recent_size: int = 30,
    baseline_size: int = 100,
    min_samples: int = 20,
) -> DriftResult:
    if recent_size <= 0 or baseline_size <= 0:
        raise ValueError("Розмір вікна має бути > 0")

    con = connect()

    try:
        rows = con.execute(
            """
            SELECT id, result_r
            FROM signals
            WHERE symbol = ?
              AND status = 'CLOSED'
              AND result_r IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (
                symbol,
                recent_size + baseline_size,
            ),
        ).fetchall()

    finally:
        con.close()

    recent_rows = rows[:recent_size]
    baseline_rows = rows[recent_size:recent_size + baseline_size]

    recent = calculate_window(recent_rows)
    baseline = calculate_window(baseline_rows)

    if (
        recent.sample_size < min_samples
        or baseline.sample_size < min_samples
    ):
        return DriftResult(
            symbol=symbol,
            state="INSUFFICIENT_DATA",
            confidence_multiplier=1.0,
            recent=recent,
            baseline=baseline,
            win_rate_change=0.0,
            average_r_change=0.0,
            reason="Недостатньо закритих сигналів для оцінки drift",
        )

    win_rate_change = recent.win_rate - baseline.win_rate
    average_r_change = recent.average_r - baseline.average_r

    if (
        win_rate_change <= -0.15
        or average_r_change <= -0.75
    ):
        state = "DRIFT"
        confidence_multiplier = 0.50
        reason = "Результативність різко погіршилась"

    elif (
        win_rate_change <= -0.08
        or average_r_change <= -0.35
    ):
        state = "WARNING"
        confidence_multiplier = 0.75
        reason = "Є ознаки погіршення поведінки моделі"

    else:
        state = "STABLE"
        confidence_multiplier = 1.0
        reason = "Суттєвого drift не виявлено"

    return DriftResult(
        symbol=symbol,
        state=state,
        confidence_multiplier=confidence_multiplier,
        recent=recent,
        baseline=baseline,
        win_rate_change=round(win_rate_change, 4),
        average_r_change=round(average_r_change, 4),
        reason=reason,
    )


if __name__ == "__main__":
    result = analyze_drift("BTC")

    print()
    print("ORACLE X — DRIFT DETECTOR")
    print("=" * 60)
    print(f"Symbol:                {result.symbol}")
    print(f"State:                 {result.state}")
    print(f"Confidence multiplier: {result.confidence_multiplier}")
    print(f"Reason:                {result.reason}")
    print()

    print("RECENT:")
    print(
        f"N={result.recent.sample_size} "
        f"WR={result.recent.win_rate:.2%} "
        f"AvgR={result.recent.average_r:.4f}"
    )

    print()

    print("BASELINE:")
    print(
        f"N={result.baseline.sample_size} "
        f"WR={result.baseline.win_rate:.2%} "
        f"AvgR={result.baseline.average_r:.4f}"
    )

    print()

    print(f"Win-rate change: {result.win_rate_change:.2%}")
    print(f"Average R change: {result.average_r_change:.4f}")
