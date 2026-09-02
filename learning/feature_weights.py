from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Dict, List

from database.db import connect


DEFAULT_WEIGHTS = {
    "regime": 0.30,
    "structure": 0.25,
    "momentum": 0.20,
    "orderflow": 0.12,
    "derivatives": 0.08,
    "liquidations": 0.05,
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
    learning_rate: float = 0.03,
    min_samples: int = 20,
) -> Dict[str, float]:
    if learning_rate <= 0 or learning_rate > 0.10:
        raise ValueError("learning_rate має бути > 0 і <= 0.10")

    current = get_weights(
        symbol=symbol,
        regime="GLOBAL",
        timeframe="MTF",
        model_version=model_version,
    )

    feature_columns = {
        "regime": "regime_component",
        "structure": "structure_component",
        "momentum": "momentum_component",
        "orderflow": "orderflow_component",
        "derivatives": "derivatives_component",
        "liquidations": "liquidations_component",
    }

    con = connect()

    try:
        rows = con.execute(
            """
            SELECT
                decision,
                result_r,
                regime_component,
                structure_component,
                momentum_component,
                orderflow_component,
                derivatives_component,
                liquidations_component
            FROM signals
            WHERE symbol = ?
              AND status = 'CLOSED'
              AND result_r IS NOT NULL
            ORDER BY id
            """,
            (symbol,),
        ).fetchall()

    finally:
        con.close()

    if not rows:
        return current

    feature_stats = {}

    for feature_name in feature_columns:
        feature_stats[feature_name] = {
            "samples": 0,
            "wins": 0,
            "losses": 0,
            "total_r": 0.0,
            "correct_direction": 0,
        }

    for row in rows:
        direction = row["decision"]

        if direction not in ("LONG", "SHORT"):
            continue

        result_r = float(row["result_r"] or 0.0)

        for feature_name, column in feature_columns.items():
            raw_value = row[column]

            if raw_value is None:
                continue

            value = float(raw_value)

            # Нульовий фактор не давав directional інформації.
            if abs(value) < 1e-9:
                continue

            stats = feature_stats[feature_name]
            stats["samples"] += 1
            stats["total_r"] += result_r

            if result_r > 0:
                stats["wins"] += 1
            elif result_r < 0:
                stats["losses"] += 1

            factor_direction = (
                "LONG"
                if value > 0
                else "SHORT"
            )

            if factor_direction == direction:
                # Якщо фактор підтримував напрям угоди:
                # прибуткова угода = правильний фактор,
                # збиткова = неправильний фактор.
                if result_r > 0:
                    stats["correct_direction"] += 1

            else:
                # Якщо фактор був проти угоди,
                # а угода програла — фактор фактично попереджав правильно.
                if result_r < 0:
                    stats["correct_direction"] += 1

    proposed = current.copy()

    for feature_name, stats in feature_stats.items():
        samples = stats["samples"]

        if samples < min_samples:
            continue

        accuracy = stats["correct_direction"] / samples
        average_r = stats["total_r"] / samples

        old_weight = current.get(
            feature_name,
            DEFAULT_WEIGHTS.get(feature_name, 0.0),
        )

        # 50% = випадкова directional точність.
        edge = accuracy - 0.50

        adjustment = edge * learning_rate * 2.0

        # Average R використовується як другий слабкий модифікатор.
        if average_r > 0:
            adjustment += min(average_r, 2.0) * learning_rate * 0.10
        elif average_r < 0:
            adjustment += max(average_r, -2.0) * learning_rate * 0.10

        # Обмежуємо зміну за один цикл.
        max_step = learning_rate
        adjustment = max(
            -max_step,
            min(max_step, adjustment),
        )

        # Не дозволяємо фактору зникнути або захопити всю модель.
        proposed[feature_name] = max(
            0.03,
            min(0.45, old_weight + adjustment),
        )

    normalized = normalize_weights(proposed)

    now = utc_now()
    con = connect()

    try:
        for feature_name, weight in normalized.items():
            stats = feature_stats.get(feature_name, {})

            samples = int(stats.get("samples", 0))
            wins = int(stats.get("wins", 0))
            losses = int(stats.get("losses", 0))

            win_rate = (
                wins / samples
                if samples > 0
                else None
            )

            average_r = (
                stats.get("total_r", 0.0) / samples
                if samples > 0
                else None
            )

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
                    samples,
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

