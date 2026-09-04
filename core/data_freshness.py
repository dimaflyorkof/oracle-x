from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict

from database.db import connect


MAX_AGE_SECONDS = {
    "5m": 10 * 60,
    "15m": 30 * 60,
    "1h": 2 * 60 * 60,
    "4h": 8 * 60 * 60,
}


@dataclass
class FreshnessResult:
    symbol: str
    status: str
    is_fresh: bool
    stale_timeframes: list[str]
    details: Dict

    def to_dict(self) -> Dict:
        return asdict(self)


def analyze_freshness(
    symbol: str = "BTC",
) -> FreshnessResult:
    now = datetime.now(timezone.utc)
    now_unix = int(now.timestamp())

    con = connect()

    details = {}
    stale_timeframes = []

    try:
        for timeframe, max_age in MAX_AGE_SECONDS.items():
            row = con.execute(
                """
                SELECT timestamp, timestamp_unix, close
                FROM market_snapshots
                WHERE symbol = ?
                  AND timeframe = ?
                ORDER BY timestamp_unix DESC
                LIMIT 1
                """,
                (
                    symbol,
                    timeframe,
                ),
            ).fetchone()

            if row is None:
                details[timeframe] = {
                    "status": "MISSING",
                    "timestamp": None,
                    "age_seconds": None,
                    "max_age_seconds": max_age,
                }

                stale_timeframes.append(timeframe)
                continue

            age_seconds = max(
                0,
                now_unix - int(row["timestamp_unix"]),
            )

            is_tf_fresh = age_seconds <= max_age

            details[timeframe] = {
                "status": (
                    "FRESH"
                    if is_tf_fresh
                    else "STALE"
                ),
                "timestamp": row["timestamp"],
                "timestamp_unix": row["timestamp_unix"],
                "age_seconds": age_seconds,
                "max_age_seconds": max_age,
                "close": row["close"],
            }

            if not is_tf_fresh:
                stale_timeframes.append(timeframe)

    finally:
        con.close()

    is_fresh = len(stale_timeframes) == 0

    return FreshnessResult(
        symbol=symbol,
        status=(
            "FRESH"
            if is_fresh
            else "STALE_DATA"
        ),
        is_fresh=is_fresh,
        stale_timeframes=stale_timeframes,
        details=details,
    )


if __name__ == "__main__":
    result = analyze_freshness("BTC")

    print()
    print("ORACLE X — DATA FRESHNESS")
    print("=" * 60)
    print("Status:", result.status)
    print("Fresh:", result.is_fresh)
    print("Stale TFs:", result.stale_timeframes)
    print()

    for timeframe, info in result.details.items():
        print(
            timeframe,
            "|",
            info["status"],
            "| age:",
            info["age_seconds"],
            "| max:",
            info["max_age_seconds"],
            "| ts:",
            info["timestamp"],
        )
