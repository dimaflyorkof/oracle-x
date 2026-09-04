from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from typing import Dict, List, Optional

from database.db import connect
from learning.feature_weights import get_weights

from core.regime import (
    Candle as RegimeCandle,
    TIMEFRAME_WEIGHTS,
    timeframe_regime,
)

from core.structure import (
    Candle as StructureCandle,
    detect_swings,
    split_swings,
    label_last_high,
    label_last_low,
    detect_break_of_structure,
    detect_change_of_character,
    classify_structure,
)

from core.momentum import (
    Candle as MomentumCandle,
    rsi,
    roc,
    impulse_ratio,
    momentum_acceleration,
    build_rsi_series,
    detect_rsi_divergence,
)


@dataclass
class ReplayPoint:
    timestamp: str
    timestamp_unix: int
    score: float
    confidence: float
    agreement: float
    bias: str
    regime_component: float
    structure_component: float
    momentum_component: float
    orderflow_component: float
    derivatives_component: float
    liquidations_component: float


def load_market_rows(symbol: str, timeframe: str) -> List:
    con = connect()
    try:
        return con.execute(
            """
            SELECT *
            FROM market_snapshots
            WHERE symbol = ?
              AND timeframe = ?
              AND open IS NOT NULL
              AND high IS NOT NULL
              AND low IS NOT NULL
              AND close IS NOT NULL
            ORDER BY timestamp_unix ASC
            """,
            (symbol, timeframe),
        ).fetchall()
    finally:
        con.close()


def build_index(rows: List) -> List[int]:
    return [int(r["timestamp_unix"]) for r in rows]


def rows_until(
    rows: List,
    index: List[int],
    ts: int,
    limit: int,
) -> List:
    pos = bisect_right(index, ts)
    start = max(0, pos - limit)
    return rows[start:pos]


def as_regime_candles(rows: List) -> List[RegimeCandle]:
    return [
        RegimeCandle(
            timestamp=r["timestamp"],
            timestamp_unix=int(r["timestamp_unix"]),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"] or 0.0),
        )
        for r in rows
    ]


def as_structure_candles(rows: List) -> List[StructureCandle]:
    return [
        StructureCandle(
            timestamp=r["timestamp"],
            timestamp_unix=int(r["timestamp_unix"]),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"] or 0.0),
        )
        for r in rows
    ]


def as_momentum_candles(rows: List) -> List[MomentumCandle]:
    return [
        MomentumCandle(
            timestamp=r["timestamp"],
            timestamp_unix=int(r["timestamp_unix"]),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=float(r["volume"] or 0.0),
        )
        for r in rows
    ]


def regime_to_score(value: str) -> float:
    if value == "TREND_UP":
        return 1.0
    if value == "TREND_DOWN":
        return -1.0
    return 0.0


def structure_to_score(value: str) -> float:
    if value == "BULLISH":
        return 1.0
    if value == "BEARISH":
        return -1.0
    return 0.0


def momentum_to_score(value: str) -> float:
    if value == "BULLISH_MOMENTUM":
        return 1.0
    if value == "BEARISH_MOMENTUM":
        return -1.0
    return 0.0


def replay_structure(rows: List) -> tuple[str, float]:
    candles = as_structure_candles(rows)

    if len(candles) < 20:
        return "RANGE", 0.0

    swings = detect_swings(candles)
    highs, lows = split_swings(swings)

    high_label = label_last_high(highs)
    low_label = label_last_low(lows)

    bos = detect_break_of_structure(
        candles,
        highs,
        lows,
    )

    detect_change_of_character(
        candles,
        highs,
        lows,
        high_label,
        low_label,
    )

    return classify_structure(
        high_label,
        low_label,
        bos,
    )


def replay_momentum(rows: List) -> tuple[str, float]:
    candles = as_momentum_candles(rows)

    if len(candles) < 40:
        return "NEUTRAL_MOMENTUM", 0.0

    closes = [c.close for c in candles]

    rsi14 = rsi(closes, 14)

    if rsi14 is None:
        return "NEUTRAL_MOMENTUM", 0.0

    roc5 = roc(closes, 5)
    roc10 = roc(closes, 10)

    impulse = impulse_ratio(candles, 10)

    momentum_acceleration(closes)

    rsi_series = build_rsi_series(
        closes,
        14,
    )

    divergence = detect_rsi_divergence(
        closes[-len(rsi_series):],
        rsi_series,
        lookback=min(20, len(rsi_series)),
    )

    score = 0.0

    if rsi14 >= 60:
        score += 1.0
    elif rsi14 <= 40:
        score -= 1.0

    if roc5 > 0:
        score += 1.0
    elif roc5 < 0:
        score -= 1.0

    if roc10 > 0:
        score += 1.0
    elif roc10 < 0:
        score -= 1.0

    if impulse >= 0.55:
        if roc5 > 0:
            score += 0.5
        elif roc5 < 0:
            score -= 0.5

    if divergence == "BULLISH_DIVERGENCE":
        score += 1.0

    if divergence == "BEARISH_DIVERGENCE":
        score -= 1.0

    if score >= 2.0:
        state = "BULLISH_MOMENTUM"
    elif score <= -2.0:
        state = "BEARISH_MOMENTUM"
    else:
        state = "NEUTRAL_MOMENTUM"

    confidence = min(
        100.0,
        abs(score) / 4.5 * 100.0,
    )

    return state, confidence


def historical_live_scores(
    symbol: str,
    ts: int,
    liquidation_window_seconds: int,
) -> Dict[str, float]:
    con = connect()

    try:
        orderflow = con.execute(
            """
            SELECT timestamp_unix, imbalance, delta
            FROM orderflow_history
            WHERE symbol = ?
              AND timestamp_unix <= ?
            ORDER BY timestamp_unix DESC
            LIMIT 1
            """,
            (symbol, ts),
        ).fetchone()

        derivatives = con.execute(
            """
            SELECT
                timestamp_unix,
                long_short_ratio,
                taker_ratio
            FROM derivatives_history
            WHERE symbol = ?
              AND source = 'binance_futures'
              AND timestamp_unix <= ?
            ORDER BY timestamp_unix DESC, id DESC
            LIMIT 1
            """,
            (symbol, ts),
        ).fetchone()

        liquidations = con.execute(
            """
            SELECT
                COALESCE(SUM(long_liquidations), 0) AS long_total,
                COALESCE(SUM(short_liquidations), 0) AS short_total
            FROM liquidation_history
            WHERE symbol = ?
              AND source = 'binance_futures'
              AND timestamp_unix > ?
              AND timestamp_unix <= ?
            """,
            (
                symbol,
                ts - liquidation_window_seconds,
                ts,
            ),
        ).fetchone()

    finally:
        con.close()

    orderflow_score = 0.0
    derivatives_score = 0.0
    liquidation_score = 0.0

    if (
        orderflow is not None
        and ts - int(orderflow["timestamp_unix"]) <= 120
    ):
        imbalance = float(orderflow["imbalance"] or 0.0)
        delta = float(orderflow["delta"] or 0.0)

        if imbalance >= 0.20 and delta > 0:
            orderflow_score = 1.0
        elif imbalance <= -0.20 and delta < 0:
            orderflow_score = -1.0
        elif delta > 0:
            orderflow_score = 0.5
        elif delta < 0:
            orderflow_score = -0.5

    if derivatives is not None:
        age = ts - int(
            derivatives["timestamp_unix"]
        )

        if age <= 3900:
            ls_ratio = float(
                derivatives["long_short_ratio"]
                or 0.0
            )
            taker_ratio = float(
                derivatives["taker_ratio"]
                or 0.0
            )

            if taker_ratio >= 1.10:
                derivatives_score += 0.6
            elif taker_ratio <= 0.90:
                derivatives_score -= 0.6

            if 1.05 <= ls_ratio < 1.20:
                derivatives_score += 0.2
            elif 0.80 < ls_ratio <= 0.95:
                derivatives_score -= 0.2

            derivatives_score = max(
                -1.0,
                min(1.0, derivatives_score),
            )

    if liquidations is not None:
        long_total = float(
            liquidations["long_total"]
            or 0.0
        )
        short_total = float(
            liquidations["short_total"]
            or 0.0
        )

        if (
            short_total > long_total * 2
            and short_total > 0
        ):
            liquidation_score = 0.5

        elif (
            long_total > short_total * 2
            and long_total > 0
        ):
            liquidation_score = -0.5

    return {
        "orderflow": orderflow_score,
        "derivatives": derivatives_score,
        "liquidations": liquidation_score,
    }


def replay_point(
    symbol: str,
    ts: int,
    data: Dict[str, List],
    indexes: Dict[str, List[int]],
    weights: Dict[str, float],
    liquidation_window_seconds: int,
) -> Optional[ReplayPoint]:
    regimes = {}

    bullish_weight = 0.0
    bearish_weight = 0.0
    neutral_weight = 0.0

    weighted_structure = 0.0
    weighted_momentum = 0.0

    for tf, tf_weight in TIMEFRAME_WEIGHTS.items():
        regime_rows = rows_until(
            data[tf],
            indexes[tf],
            ts,
            120,
        )

        structure_rows = rows_until(
            data[tf],
            indexes[tf],
            ts,
            250,
        )

        momentum_rows = rows_until(
            data[tf],
            indexes[tf],
            ts,
            120,
        )

        if len(regime_rows) < 60:
            return None

        regime = timeframe_regime(
            tf,
            as_regime_candles(regime_rows),
        )

        structure, structure_conf = replay_structure(
            structure_rows
        )

        momentum, momentum_conf = replay_momentum(
            momentum_rows
        )

        regimes[tf] = regime

        structure_component = (
            structure_to_score(structure)
            * (structure_conf / 100.0)
        )

        momentum_component = (
            momentum_to_score(momentum)
            * (momentum_conf / 100.0)
        )

        weighted_structure += (
            structure_component * tf_weight
        )

        weighted_momentum += (
            momentum_component * tf_weight
        )

        combined_tf = (
            structure_component
            + momentum_component
        )

        if combined_tf > 0.25:
            bullish_weight += tf_weight
        elif combined_tf < -0.25:
            bearish_weight += tf_weight
        else:
            neutral_weight += tf_weight

    weighted_regime_score = sum(
        regimes[tf].direction_score
        * TIMEFRAME_WEIGHTS[tf]
        for tf in TIMEFRAME_WEIGHTS
    )

    if weighted_regime_score >= 1.25:
        overall_regime = "TREND_UP"
    elif weighted_regime_score <= -1.25:
        overall_regime = "TREND_DOWN"
    else:
        overall_regime = "RANGE"

    regime_weights = {
        "TREND_UP": 0.0,
        "TREND_DOWN": 0.0,
        "RANGE": 0.0,
    }

    for tf, tf_weight in TIMEFRAME_WEIGHTS.items():
        regime_weights[
            regimes[tf].regime
        ] += tf_weight

    regime_agreement = (
        max(regime_weights.values())
        * 100.0
    )

    directional_strength = min(
        100.0,
        abs(weighted_regime_score)
        / 4.0
        * 100.0,
    )

    regime_confidence = (
        directional_strength * 0.65
        + regime_agreement * 0.35
    )

    regime_component = (
        regime_to_score(overall_regime)
        * (regime_confidence / 100.0)
    )

    live = historical_live_scores(
        symbol,
        ts,
        liquidation_window_seconds,
    )

    raw_score = (
        regime_component
        * weights.get("regime", 0.30)
        + weighted_structure
        * weights.get("structure", 0.25)
        + weighted_momentum
        * weights.get("momentum", 0.20)
        + live["orderflow"]
        * weights.get("orderflow", 0.12)
        + live["derivatives"]
        * weights.get("derivatives", 0.08)
        + live["liquidations"]
        * weights.get("liquidations", 0.05)
    )

    score = raw_score * 100.0

    agreement = max(
        bullish_weight,
        bearish_weight,
        neutral_weight,
    ) * 100.0

    confidence = min(
        100.0,
        abs(score) * 0.70
        + agreement * 0.30,
    )

    if score >= 20:
        bias = "BULLISH"
    elif score <= -20:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"

    row_15m = rows_until(
        data["15m"],
        indexes["15m"],
        ts,
        1,
    )

    if not row_15m:
        return None

    return ReplayPoint(
        timestamp=row_15m[-1]["timestamp"],
        timestamp_unix=ts,
        score=round(score, 2),
        confidence=round(confidence, 2),
        agreement=round(agreement, 2),
        bias=bias,
        regime_component=round(
            regime_component,
            4,
        ),
        structure_component=round(
            weighted_structure,
            4,
        ),
        momentum_component=round(
            weighted_momentum,
            4,
        ),
        orderflow_component=live["orderflow"],
        derivatives_component=live["derivatives"],
        liquidations_component=live["liquidations"],
    )


def run_replay(
    symbol: str = "BTC",
    liquidation_window_seconds: int = 900,
) -> List[ReplayPoint]:
    data = {
        tf: load_market_rows(symbol, tf)
        for tf in ("15m", "1h", "4h")
    }

    indexes = {
        tf: build_index(rows)
        for tf, rows in data.items()
    }

    weights = get_weights(symbol)

    con = connect()

    try:
        overlap = con.execute(
            """
            SELECT MAX(first_ts) AS start_ts
            FROM (
                SELECT MIN(timestamp_unix) AS first_ts
                FROM orderflow_history
                WHERE symbol = ?

                UNION ALL

                SELECT MIN(timestamp_unix) AS first_ts
                FROM derivatives_history
                WHERE symbol = ?
                  AND source = 'binance_futures'

                UNION ALL

                SELECT MIN(timestamp_unix) AS first_ts
                FROM liquidation_history
                WHERE symbol = ?
                  AND source = 'binance_futures'
            )
            """,
            (symbol, symbol, symbol),
        ).fetchone()

    finally:
        con.close()

    if overlap is None or overlap["start_ts"] is None:
        return []

    start_ts = int(overlap["start_ts"])

    results = []

    for row in data["15m"]:
        ts = int(row["timestamp_unix"])

        if ts < start_ts:
            continue

        point = replay_point(
            symbol,
            ts,
            data,
            indexes,
            weights,
            liquidation_window_seconds,
        )

        if point is not None:
            results.append(point)

    return results


def summarize(
    label: str,
    results: List[ReplayPoint],
):
    bullish = sum(
        1 for r in results
        if r.bias == "BULLISH"
    )
    bearish = sum(
        1 for r in results
        if r.bias == "BEARISH"
    )
    neutral = sum(
        1 for r in results
        if r.bias == "NEUTRAL"
    )

    print()
    print(label)
    print("-" * 80)
    print("Replay points:", len(results))

    if results:
        print(
            "Range:",
            results[0].timestamp,
            "->",
            results[-1].timestamp,
        )

    print("BULLISH:", bullish)
    print("BEARISH:", bearish)
    print("NEUTRAL:", neutral)


def main():
    print()
    print(
        "ORACLE X — SIX-FACTOR REPLAY "
        "LIQUIDATION WINDOW TEST"
    )
    print("=" * 80)

    for minutes in (15, 30, 60):
        results = run_replay(
            symbol="BTC",
            liquidation_window_seconds=minutes * 60,
        )

        summarize(
            f"LIQ WINDOW = {minutes} MIN",
            results,
        )


if __name__ == "__main__":
    main()
