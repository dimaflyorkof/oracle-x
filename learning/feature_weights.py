from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List

from database.db import connect


DEFAULT_WEIGHTS = {
    "regime": 0.40,
    "structure": 0.35,
    "momentum": 0.25,
}


@dataclass
class FeatureWeight:
    symbol: str
    regime: str
    timeframe: str
    feature_name: str
    weight: float
    sample_size: int
    wins: int
    losses: int
    win_rate: float | None
    average_r: float | None
    model_version: str

    def to_dict(self) -> Dict:
        return asdict(self)


def utc_now():
    return datetime.now(timezone.utc)


def ensure_default_weights(
    symbol: str = "BTC",
    regime: str = "GLOBAL",
    timeframe: str = "MTF",
    model_version: str = "1.0",
) -> None:
    now = utc_now()

    con = connect()

    try:
        for feature_name, weight in DEFAULT_WEIGHTS.items():
            con.execute(
                """
                INSERT OR IGNORE INTO feature_weights (
                    timestamp,
                    timestamp_unix,
                    symbol,
                    regime,
                    timeframe,
                    feature_name,
                    weight,
                    sample_size,
                    wins,
                    losses,
                    win_rate,
                    average_r,
                    model_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, NULL, NULL, ?)
                """,
                (
                    now.isoformat(),
                    int(now.timestamp()),
                    symbol,
                    regime,
                    timeframe,
                    feature_name,
                    weight,
                    model_version,
                ),
            )

        con.commit()

    finally:
        con.close()


def get_weights(
    symbol: str = "BTC",
    regime: str = "GLOBAL",
    timeframe: str = "MTF",
    model_version: str = "1.0",
) -> Dict[str, float]:
    ensure_default_weights(
        symbol=symbol,
        regime=regime,
        timeframe=timeframe,
        model_version=model_version,
    )

    con = connect()

    try:
        rows = con.execute(
            """
            SELECT feature_name, weight
            FROM feature_weights
            WHERE symbol = ?
              AND regime = ?
              AND timeframe = ?
              AND model_version = ?
            ORDER BY feature_name
            """,
            (
                symbol,
                regime,
                timeframe,
                model_version,
            ),
        ).fetchall()

    finally:
        con.close()

    return {
        row["feature_name"]: float(row["weight"])
        for row in rows
    }


def get_full_weight_state(
    symbol: str = "BTC",
    regime: str = "GLOBAL",
    timeframe: str = "MTF",
    model_version: str = "1.0",
) -> List[FeatureWeight]:
    ensure_default_weights(
        symbol=symbol,
        regime=regime,
        timeframe=timeframe,
        model_version=model_version,
    )

    con = connect()

    try:
        rows = con.execute(
            """
            SELECT *
            FROM feature_weights
            WHERE symbol = ?
              AND regime = ?
              AND timeframe = ?
              AND model_version = ?
            ORDER BY feature_name
            """,
            (
                symbol,
                regime,
                timeframe,
                model_version,
            ),
        ).fetchall()

    finally:
        con.close()

    return [
        FeatureWeight(
            symbol=row["symbol"],
            regime=row["regime"],
            timeframe=row["timeframe"],
            feature_name=row["feature_name"],
            weight=float(row["weight"]),
            sample_size=int(row["sample_size"] or 0),
            wins=int(row["wins"] or 0),
            losses=int(row["losses"] or 0),
            win_rate=(
                float(row["win_rate"])
                if row["win_rate"] is not None
                else None
            ),
            average_r=(
                float(row["average_r"])
                if row["average_r"] is not None
                else None
            ),
            model_version=row["model_version"],
        )
        for row in rows
    ]


def normalize_weights(
    weights: Dict[str, float],
) -> Dict[str, float]:
    positive = {
        key: max(float(value), 0.0)
        for key, value in weights.items()
    }

    total = sum(positive.values())

    if total <= 0:
        return DEFAULT_WEIGHTS.copy()

    return {
        key: value / total
        for key, value in positive.items()
    }


def update_weight(
    feature_name: str,
    new_weight: float,
    symbol: str = "BTC",
    regime: str = "GLOBAL",
    timeframe: str = "MTF",
    model_version: str = "1.0",
) -> None:
    if feature_name not in DEFAULT_WEIGHTS:
        raise ValueError(
            f"Невідомий feature_name: {feature_name}"
        )

    ensure_default_weights(
        symbol=symbol,
        regime=regime,
        timeframe=timeframe,
        model_version=model_version,
    )

    now = utc_now()

    con = connect()

    try:
        con.execute(
            """
            UPDATE feature_weights
            SET weight = ?,
                timestamp = ?,
                timestamp_unix = ?
            WHERE symbol = ?
              AND regime = ?
              AND timeframe = ?
              AND feature_name = ?
              AND model_version = ?
            """,
            (
                float(new_weight),
                now.isoformat(),
                int(now.timestamp()),
                symbol,
                regime,
                timeframe,
                feature_name,
                model_version,
            ),
        )

        con.commit()

    finally:
        con.close()


if __name__ == "__main__":
    ensure_default_weights("BTC")

    print()
    print("ORACLE X — FEATURE WEIGHTS")
    print("=" * 50)

    weights = get_weights("BTC")

    for feature, weight in weights.items():
        print(f"{feature:<12} {weight:.4f}")


def learn_from_closed_signals(
    symbol: str = "BTC",
    model_version: str = "1.0",
    learning_rate: float = 0.05,
) -> Dict[str, float]:
    if learning_rate <= 0 or learning_rate > 0.25:
        raise ValueError("learning_rate має бути > 0 і <= 0.25")

    current = get_weights(
        symbol=symbol,
        regime="GLOBAL",
        timeframe="MTF",
        model_version=model_version,
    )

    con = connect()

    try:
        rows = con.execute(
            """
            SELECT
                decision,
                result,
                result_r,
                market_regime
            FROM signals
            WHERE symbol = ?
              AND status = 'CLOSED'
              AND result IS NOT NULL
              AND result_r IS NOT NULL
            ORDER BY id
            """,
            (symbol,),
        ).fetchall()

    finally:
        con.close()

    if not rows:
        return current

    wins = 0
    losses = 0
    total_r = 0.0

    for row in rows:
        result_r = float(row["result_r"] or 0.0)
        total_r += result_r

        if result_r > 0:
            wins += 1
        elif result_r < 0:
            losses += 1

    sample_size = wins + losses

    if sample_size == 0:
        return current

    win_rate = wins / sample_size
    average_r = total_r / sample_size

    # Поки що використовуємо дуже консервативне навчання.
    # Якщо статистика позитивна — трохи підсилюємо structure/regime.
    # Якщо негативна — трохи збільшуємо вагу momentum як фільтра.
    proposed = current.copy()

    if average_r > 0 and win_rate >= 0.55:
        proposed["regime"] = current.get("regime", 0.40) + learning_rate * 0.5
        proposed["structure"] = current.get("structure", 0.35) + learning_rate * 0.5
        proposed["momentum"] = current.get("momentum", 0.25) - learning_rate

    elif average_r < 0 or win_rate < 0.45:
        proposed["regime"] = current.get("regime", 0.40) - learning_rate * 0.5
        proposed["structure"] = current.get("structure", 0.35) - learning_rate * 0.5
        proposed["momentum"] = current.get("momentum", 0.25) + learning_rate

    normalized = normalize_weights(proposed)

    now = utc_now()
    con = connect()

    try:
        for feature_name, weight in normalized.items():
            con.execute(
                """
                UPDATE feature_weights
                SET weight = ?,
                    sample_size = ?,
                    wins = ?,
                    losses = ?,
                    win_rate = ?,
                    average_r = ?,
                    timestamp = ?,
                    timestamp_unix = ?
                WHERE symbol = ?
                  AND regime = 'GLOBAL'
                  AND timeframe = 'MTF'
                  AND feature_name = ?
                  AND model_version = ?
                """,
                (
                    float(weight),
                    sample_size,
                    wins,
                    losses,
                    win_rate,
                    average_r,
                    now.isoformat(),
                    int(now.timestamp()),
                    symbol,
                    feature_name,
                    model_version,
                ),
            )

        con.commit()

    finally:
        con.close()

    return normalized
