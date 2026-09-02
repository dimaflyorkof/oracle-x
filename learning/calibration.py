from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List

from database.db import connect


@dataclass
class CalibrationBucket:
    lower: int
    upper: int
    sample_size: int
    wins: int
    losses: int
    expected_probability: float
    actual_win_rate: float
    calibration_error: float

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class CalibrationResult:
    symbol: str
    total_samples: int
    weighted_error: float
    buckets: List[CalibrationBucket]

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "total_samples": self.total_samples,
            "weighted_error": self.weighted_error,
            "buckets": [
                bucket.to_dict()
                for bucket in self.buckets
            ],
        }


BUCKETS = [
    (0, 20),
    (20, 40),
    (40, 60),
    (60, 80),
    (80, 101),
]


def analyze_calibration(
    symbol: str = "BTC",
) -> CalibrationResult:
    con = connect()

    try:
        rows = con.execute(
            """
            SELECT confidence, result_r
            FROM signals
            WHERE symbol = ?
              AND status = 'CLOSED'
              AND confidence IS NOT NULL
              AND result_r IS NOT NULL
            """,
            (symbol,),
        ).fetchall()

    finally:
        con.close()

    result_buckets: List[CalibrationBucket] = []

    total_samples = 0
    weighted_error_sum = 0.0

    for lower, upper in BUCKETS:
        selected = [
            row for row in rows
            if lower <= float(row["confidence"]) < upper
        ]

        sample_size = len(selected)

        if sample_size == 0:
            continue

        wins = sum(
            1
            for row in selected
            if float(row["result_r"]) > 0
        )

        losses = sum(
            1
            for row in selected
            if float(row["result_r"]) < 0
        )

        expected_probability = (
            sum(float(row["confidence"]) for row in selected)
            / sample_size
            / 100.0
        )

        actual_win_rate = wins / sample_size

        calibration_error = abs(
            actual_win_rate - expected_probability
        )

        result_buckets.append(
            CalibrationBucket(
                lower=lower,
                upper=upper - 1,
                sample_size=sample_size,
                wins=wins,
                losses=losses,
                expected_probability=round(
                    expected_probability,
                    4,
                ),
                actual_win_rate=round(
                    actual_win_rate,
                    4,
                ),
                calibration_error=round(
                    calibration_error,
                    4,
                ),
            )
        )

        total_samples += sample_size
        weighted_error_sum += (
            calibration_error * sample_size
        )

    weighted_error = (
        weighted_error_sum / total_samples
        if total_samples > 0
        else 0.0
    )

    return CalibrationResult(
        symbol=symbol,
        total_samples=total_samples,
        weighted_error=round(weighted_error, 4),
        buckets=result_buckets,
    )


if __name__ == "__main__":
    result = analyze_calibration("BTC")

    print()
    print("ORACLE X — CONFIDENCE CALIBRATION")
    print("=" * 60)
    print(f"Symbol:          {result.symbol}")
    print(f"Samples:         {result.total_samples}")
    print(f"Weighted error:  {result.weighted_error}")
    print()

    if not result.buckets:
        print("Недостатньо закритих сигналів для калібрування")
    else:
        for bucket in result.buckets:
            print(
                f"{bucket.lower:02d}-{bucket.upper:02d}% | "
                f"N={bucket.sample_size:<4} | "
                f"expected={bucket.expected_probability:.2%} | "
                f"actual={bucket.actual_win_rate:.2%} | "
                f"error={bucket.calibration_error:.2%}"
            )
