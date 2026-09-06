from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Dict, Iterable, List, Optional, Tuple

from database.db import connect


SYMBOL = "BTC"
ORDERFLOW_SOURCES = (
    "binance_spot_kline_15m",
    "binance_futures_kline_15m",
)
DERIVATIVE_SOURCES = (
    "binance_futures",
    "bybit_futures",
    "okx_futures",
)

BASE_WEIGHTS = {
    "orderflow": 0.30,
    "derivatives": 0.25,
    "macro": 0.20,
    "onchain": 0.10,
    "sentiment": 0.05,
    "liquidations": 0.10,
}


@dataclass
class Component:
    name: str
    status: str
    active: bool
    score: float
    confidence: float
    observations: int
    latest_timestamp_unix: Optional[int]
    age_seconds: Optional[int]
    details: Dict


@dataclass
class IntelligenceResult:
    symbol: str
    as_of_unix: int
    decision: str
    market_state: str
    flow_state: str
    score: float
    confidence: float
    data_coverage: float
    contradictions: List[str]
    components: Dict[str, Dict]
    unavailable: Dict[str, str]


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def average(values: Iterable[float]) -> float:
    cleaned = [float(value) for value in values if value is not None]
    return sum(cleaned) / len(cleaned) if cleaned else 0.0


def robust_z(values: List[float], current: Optional[float] = None) -> float:
    cleaned = [float(value) for value in values if value is not None]
    if not cleaned:
        return 0.0
    target = float(current) if current is not None else cleaned[-1]
    history = cleaned[:-1] if current is None else cleaned
    if len(history) < 8:
        return 0.0
    median = statistics.median(history)
    deviations = [abs(value - median) for value in history]
    mad = statistics.median(deviations)
    if mad > 1e-12:
        return clamp((target - median) / (1.4826 * mad), -3.0, 3.0)
    deviation = statistics.pstdev(history)
    if deviation <= 1e-12:
        return 0.0
    return clamp((target - median) / deviation, -3.0, 3.0)


def component(
    name: str,
    status: str,
    active: bool,
    score: float,
    confidence: float,
    observations: int,
    latest_ts: Optional[int],
    as_of_ts: int,
    details: Dict,
) -> Component:
    age = None if latest_ts is None else max(0, as_of_ts - int(latest_ts))
    return Component(
        name=name,
        status=status,
        active=active,
        score=round(clamp(score), 6),
        confidence=round(clamp(confidence, 0.0, 1.0), 6),
        observations=int(observations),
        latest_timestamp_unix=latest_ts,
        age_seconds=age,
        details=details,
    )


def price_change(as_of_ts: int, hours: int = 4) -> float:
    cutoff = as_of_ts - 15 * 60
    con = connect()
    try:
        rows = con.execute(
            """
            SELECT close
            FROM market_snapshots
            WHERE symbol = ?
              AND timeframe = '15m'
              AND timestamp_unix <= ?
              AND close IS NOT NULL
            ORDER BY timestamp_unix DESC
            LIMIT ?
            """,
            (SYMBOL, cutoff, hours * 4 + 1),
        ).fetchall()
    finally:
        con.close()
    if len(rows) < 2:
        return 0.0
    newest = float(rows[0]["close"])
    oldest = float(rows[-1]["close"])
    return newest / oldest - 1.0 if oldest else 0.0


def score_orderflow(as_of_ts: int) -> Component:
    # Kline rows become available only when their 15m candle closes.
    cutoff = as_of_ts - 15 * 60
    source_scores = {}
    latest_ts = None
    total_observations = 0
    con = connect()
    try:
        for source in ORDERFLOW_SOURCES:
            rows = con.execute(
                """
                SELECT timestamp_unix, buy_volume, sell_volume, delta
                FROM orderflow_history
                WHERE symbol = ?
                  AND source = ?
                  AND timestamp_unix <= ?
                  AND buy_volume IS NOT NULL
                  AND sell_volume IS NOT NULL
                ORDER BY timestamp_unix DESC
                LIMIT 96
                """,
                (SYMBOL, source, cutoff),
            ).fetchall()
            if len(rows) < 16:
                continue
            rows = list(reversed(rows))
            total_observations += len(rows)
            latest_ts = max(latest_ts or 0, int(rows[-1]["timestamp_unix"]))
            windows = []
            for length, weight in ((4, 0.50), (16, 0.30), (96, 0.20)):
                selected = rows[-min(length, len(rows)):]
                buy = sum(float(row["buy_volume"] or 0.0) for row in selected)
                sell = sum(float(row["sell_volume"] or 0.0) for row in selected)
                total = buy + sell
                imbalance = (buy - sell) / total if total > 0 else 0.0
                windows.append((clamp(imbalance * 12.0), weight, imbalance))
            weighted = sum(value * weight for value, weight, _ in windows)
            source_scores[source] = {
                "score": round(weighted, 6),
                "imbalance_1h": round(windows[0][2], 6),
                "imbalance_4h": round(windows[1][2], 6),
                "imbalance_24h": round(windows[2][2], 6),
                "rows": len(rows),
            }
    finally:
        con.close()

    if len(source_scores) < 2:
        return component(
            "orderflow", "INSUFFICIENT_HISTORY", False, 0.0, 0.0,
            total_observations, latest_ts, as_of_ts, source_scores,
        )
    spot = source_scores[ORDERFLOW_SOURCES[0]]["score"]
    futures = source_scores[ORDERFLOW_SOURCES[1]]["score"]
    score = 0.60 * spot + 0.40 * futures
    agreement = 1.0 if spot * futures >= 0 else 0.55
    freshness = 1.0 if latest_ts and as_of_ts - latest_ts <= 1800 else 0.7
    return component(
        "orderflow", "ACTIVE", True, score,
        agreement * freshness, total_observations, latest_ts, as_of_ts,
        source_scores,
    )


def completed_hourly_derivative_rows(
    raw_rows: List,
    as_of_ts: int,
    limit: int = 240,
) -> List[Dict]:
    """Build comparable, fully available hourly observations.

    Historical taker/position rows were aggregated over a complete hour but
    stamped at that hour's open.  Live rows arrive every five minutes.  Both
    become usable here only after the represented UTC hour has ended.
    """
    grouped: Dict[int, List[Dict]] = {}
    for raw_row in raw_rows:
        row = dict(raw_row)
        event_ts = int(
            row.get("event_timestamp_unix")
            or row["timestamp_unix"]
        )
        hour_open = event_ts // 3600 * 3600
        if hour_open + 3600 > as_of_ts:
            continue
        grouped.setdefault(hour_open, []).append(row)

    result = []
    average_fields = (
        "funding_rate",
        "long_short_ratio",
        "taker_ratio",
        "futures_basis",
    )
    for hour_open in sorted(grouped):
        group = sorted(
            grouped[hour_open],
            key=lambda row: int(
                row.get("available_at_unix")
                or row["timestamp_unix"]
            ),
        )
        item: Dict = {
            "timestamp_unix": hour_open,
            "available_at_unix": hour_open + 3600,
        }
        for field in average_fields:
            values = [
                float(row[field])
                for row in group
                if row.get(field) is not None
            ]
            item[field] = average(values) if values else None
        oi_values = [
            float(row["open_interest"])
            for row in group
            if row.get("open_interest") is not None
        ]
        item["open_interest"] = oi_values[-1] if oi_values else None
        item["open_interest_change"] = None
        result.append(item)

    result = result[-limit:]
    for index, row in enumerate(result):
        current_oi = row.get("open_interest")
        if current_oi is None:
            continue
        target_ts = int(row["timestamp_unix"]) - 4 * 3600
        previous = None
        for candidate in reversed(result[:index]):
            if int(candidate["timestamp_unix"]) <= target_ts:
                previous = candidate
                break
        previous_oi = previous.get("open_interest") if previous else None
        if previous_oi not in (None, 0):
            row["open_interest_change"] = float(current_oi) / float(previous_oi) - 1.0
    return result


def changed_values(values: Iterable[float]) -> List[float]:
    """Remove copied funding values without changing causal order."""
    result = []
    previous = None
    for value in values:
        current = float(value)
        if previous is None or current != previous:
            result.append(current)
        previous = current
    return result


def score_derivatives(as_of_ts: int) -> Component:
    price_move = price_change(as_of_ts, 4)
    source_results = {}
    latest_ts = None
    observations = 0
    con = connect()
    try:
        for source in DERIVATIVE_SOURCES:
            rows = con.execute(
                """
                SELECT timestamp_unix,
                       event_timestamp_unix,
                       available_at_unix,
                       funding_rate,
                       open_interest,
                       open_interest_change,
                       long_short_ratio,
                       taker_ratio,
                       futures_basis
                FROM derivatives_history
                WHERE symbol = ?
                  AND source = ?
                  AND available_at_unix <= ?
                ORDER BY available_at_unix DESC, id DESC
                LIMIT 3500
                """,
                (SYMBOL, source, as_of_ts),
            ).fetchall()
            rows = completed_hourly_derivative_rows(rows, as_of_ts)
            if len(rows) < 20:
                continue
            source_latest_ts = int(rows[-1]["available_at_unix"])
            source_age_seconds = max(0, as_of_ts - source_latest_ts)
            # Never let an old exchange snapshot influence a live decision
            # merely because another exchange is fresh.
            if source_age_seconds > 6 * 3600:
                continue
            observations += len(rows)
            latest_ts = max(
                latest_ts or 0,
                source_latest_ts,
            )
            signals = {}

            funding = changed_values(
                row["funding_rate"]
                for row in rows
                if row["funding_rate"] is not None
            )
            if len(funding) >= 8:
                signals["funding_contrarian"] = -clamp(robust_z(funding) / 2.0)

            oi = [
                row["open_interest_change"]
                for row in rows if row["open_interest_change"] is not None
            ]
            if len(oi) >= 8 and abs(price_move) > 1e-9:
                oi_intensity = abs(clamp(robust_z(oi) / 2.0))
                signals["oi_with_price"] = (
                    oi_intensity if price_move > 0 else -oi_intensity
                )

            taker = [row["taker_ratio"] for row in rows if row["taker_ratio"] is not None]
            if len(taker) >= 8:
                signals["taker_flow"] = clamp(robust_z(taker) / 2.0)

            long_short = [
                row["long_short_ratio"]
                for row in rows if row["long_short_ratio"] is not None
            ]
            if len(long_short) >= 8:
                signals["crowding_contrarian"] = -clamp(
                    robust_z(long_short) / 2.0
                )

            basis = [row["futures_basis"] for row in rows if row["futures_basis"] is not None]
            if len(basis) >= 8:
                signals["basis"] = clamp(robust_z(basis) / 3.0)

            if signals:
                source_results[source] = {
                    "score": round(average(signals.values()), 6),
                    "signals": {key: round(value, 6) for key, value in signals.items()},
                    "rows": len(rows),
                    "latest_available_at_unix": source_latest_ts,
                    "age_seconds": source_age_seconds,
                }
    finally:
        con.close()

    if not source_results:
        return component(
            "derivatives", "INSUFFICIENT_HISTORY", False, 0.0, 0.0,
            observations, latest_ts, as_of_ts, {"price_change_4h": price_move},
        )
    scores = [item["score"] for item in source_results.values()]
    signs = [1 if value > 0 else -1 if value < 0 else 0 for value in scores]
    agreement = abs(sum(signs)) / max(1, len(signs))
    freshness = 1.0 if latest_ts and as_of_ts - latest_ts <= 7200 else 0.6
    details = {
        "price_change_4h": round(price_move, 6),
        "sources": source_results,
    }
    return component(
        "derivatives", "ACTIVE", True, average(scores),
        (0.55 + 0.45 * agreement) * freshness,
        observations, latest_ts, as_of_ts, details,
    )


def daily_rows(table: str, as_of_ts: int, limit: int = 90) -> List:
    allowed = {"macro_history", "onchain_history", "sentiment_history"}
    if table not in allowed:
        raise ValueError("Unsupported daily table")
    # Daily observations are conservatively usable one day after their stamp.
    cutoff = as_of_ts - 24 * 60 * 60
    con = connect()
    try:
        rows = con.execute(
            f"SELECT * FROM {table} "
            "WHERE timestamp_unix <= ? "
            "ORDER BY timestamp_unix DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return list(reversed(rows))
    finally:
        con.close()


def return_z(rows: List, field: str) -> float:
    values = [float(row[field]) for row in rows if row[field] is not None]
    if len(values) < 10:
        return 0.0
    returns = [
        values[i] / values[i - 1] - 1.0
        for i in range(1, len(values))
        if values[i - 1] != 0
    ]
    return robust_z(returns) if len(returns) >= 8 else 0.0


def score_macro(as_of_ts: int) -> Component:
    rows = daily_rows("macro_history", as_of_ts)
    latest_ts = int(rows[-1]["timestamp_unix"]) if rows else None
    if len(rows) < 30:
        return component(
            "macro", "INSUFFICIENT_HISTORY", False, 0.0, 0.0,
            len(rows), latest_ts, as_of_ts, {},
        )
    directions = {
        "dxy": -1.0,
        "nasdaq": 1.0,
        "sp500": 1.0,
        "vix": -1.0,
        "us10y": -1.0,
        "total_market_cap": 1.0,
    }
    signals = {}
    for field, direction in directions.items():
        value = return_z(rows, field)
        if value != 0.0:
            signals[field] = clamp(direction * value / 2.0)
    if not signals:
        return component(
            "macro", "INSUFFICIENT_VALUES", False, 0.0, 0.0,
            len(rows), latest_ts, as_of_ts, {},
        )
    score = average(signals.values())
    positive = sum(1 for value in signals.values() if value > 0)
    negative = sum(1 for value in signals.values() if value < 0)
    agreement = max(positive, negative) / len(signals)
    return component(
        "macro", "ACTIVE", True, score, 0.5 + 0.5 * agreement,
        len(rows), latest_ts, as_of_ts,
        {"signals": {key: round(value, 6) for key, value in signals.items()}},
    )


def score_onchain(as_of_ts: int) -> Component:
    rows = daily_rows("onchain_history", as_of_ts)
    latest_ts = int(rows[-1]["timestamp_unix"]) if rows else None
    if len(rows) < 30:
        return component(
            "onchain", "INSUFFICIENT_HISTORY", False, 0.0, 0.0,
            len(rows), latest_ts, as_of_ts, {},
        )
    directions = {
        "exchange_netflow": -1.0,
        "stablecoin_flow": 1.0,
        "miner_flow": -1.0,
    }
    signals = {}
    for field, direction in directions.items():
        values = [float(row[field]) for row in rows if row[field] is not None]
        if len(values) >= 10:
            signals[field] = clamp(direction * robust_z(values) / 2.0)
    if not signals:
        return component(
            "onchain", "INSUFFICIENT_VALUES", False, 0.0, 0.0,
            len(rows), latest_ts, as_of_ts, {},
        )
    return component(
        "onchain", "ACTIVE", True, average(signals.values()), 0.65,
        len(rows), latest_ts, as_of_ts,
        {"signals": {key: round(value, 6) for key, value in signals.items()}},
    )


def normalized_sentiment(values: List[float]) -> float:
    if not values:
        return 0.0
    current = values[-1]
    low = min(values)
    high = max(values)
    if low >= 0.0 and high <= 100.0:
        return clamp((current - 50.0) / 35.0)
    if low >= -1.0 and high <= 1.0:
        return clamp(current)
    return clamp(robust_z(values) / 2.0)


def score_sentiment(as_of_ts: int) -> Component:
    rows = daily_rows("sentiment_history", as_of_ts)
    latest_ts = int(rows[-1]["timestamp_unix"]) if rows else None
    if len(rows) < 30:
        return component(
            "sentiment", "INSUFFICIENT_HISTORY", False, 0.0, 0.0,
            len(rows), latest_ts, as_of_ts, {},
        )
    values = [
        float(row["sentiment_score"])
        for row in rows if row["sentiment_score"] is not None
    ]
    if len(values) < 20:
        return component(
            "sentiment", "INSUFFICIENT_VALUES", False, 0.0, 0.0,
            len(rows), latest_ts, as_of_ts, {},
        )
    score = normalized_sentiment(values)
    return component(
        "sentiment", "ACTIVE", True, score, 0.55,
        len(rows), latest_ts, as_of_ts,
        {"latest_normalized": round(score, 6)},
    )


def score_liquidations(as_of_ts: int) -> Component:
    con = connect()
    try:
        coverage = con.execute(
            """
            SELECT COUNT(*) AS n, MIN(timestamp_unix) AS first_ts,
                   MAX(timestamp_unix) AS last_ts
            FROM liquidation_history
            WHERE symbol = ? AND timestamp_unix <= ?
            """,
            (SYMBOL, as_of_ts),
        ).fetchone()
    finally:
        con.close()
    observations = int(coverage["n"] or 0)
    first_ts = coverage["first_ts"]
    last_ts = coverage["last_ts"]
    days = (
        (int(last_ts) - int(first_ts)) / 86400.0
        if first_ts is not None and last_ts is not None else 0.0
    )
    if days < 30.0:
        return component(
            "liquidations", "INSUFFICIENT_HISTORY", False, 0.0, 0.0,
            observations, int(last_ts) if last_ts is not None else None,
            as_of_ts, {"history_days": round(days, 2), "required_days": 30},
        )

    con = connect()
    try:
        row = con.execute(
            """
            SELECT SUM(long_liquidations) AS long_liq,
                   SUM(short_liquidations) AS short_liq
            FROM liquidation_history
            WHERE symbol = ?
              AND timestamp_unix > ?
              AND timestamp_unix <= ?
            """,
            (SYMBOL, as_of_ts - 3600, as_of_ts),
        ).fetchone()
    finally:
        con.close()
    long_liq = float(row["long_liq"] or 0.0)
    short_liq = float(row["short_liq"] or 0.0)
    total = long_liq + short_liq
    score = (short_liq - long_liq) / total if total > 0 else 0.0
    return component(
        "liquidations", "ACTIVE", True, score, 0.55,
        observations, int(last_ts), as_of_ts,
        {"long_1h": long_liq, "short_1h": short_liq},
    )


def analyze_market_intelligence(
    symbol: str = SYMBOL,
    as_of_ts: Optional[int] = None,
) -> IntelligenceResult:
    if symbol != SYMBOL:
        raise ValueError("Market Intelligence v1 currently supports BTC only")
    as_of = int(as_of_ts if as_of_ts is not None else time.time())
    items = {
        "orderflow": score_orderflow(as_of),
        "derivatives": score_derivatives(as_of),
        "macro": score_macro(as_of),
        "onchain": score_onchain(as_of),
        "sentiment": score_sentiment(as_of),
        "liquidations": score_liquidations(as_of),
    }

    active_weight = sum(
        BASE_WEIGHTS[name]
        for name, item in items.items() if item.active
    )
    total_weight = sum(BASE_WEIGHTS.values())
    score = (
        sum(
            item.score * BASE_WEIGHTS[name] * item.confidence
            for name, item in items.items() if item.active
        )
        / sum(
            BASE_WEIGHTS[name] * item.confidence
            for name, item in items.items() if item.active
        )
        if any(item.active and item.confidence > 0 for item in items.values())
        else 0.0
    )
    coverage = active_weight / total_weight if total_weight else 0.0

    contradictions = []
    active_items = [item for item in items.values() if item.active]
    bullish = [item.name for item in active_items if item.score >= 0.25]
    bearish = [item.name for item in active_items if item.score <= -0.25]
    if bullish and bearish:
        contradictions.append(
            "Bullish components: " + ", ".join(bullish)
            + "; bearish components: " + ", ".join(bearish)
        )

    if score >= 0.18:
        decision = "LONG_ALLOWED"
        state = "RISK_ON"
    elif score <= -0.18:
        decision = "SHORT_ALLOWED"
        state = "RISK_OFF"
    else:
        decision = "BLOCK"
        state = "NEUTRAL"

    orderflow_score = items["orderflow"].score
    derivatives_score = items["derivatives"].score
    flow_score = average((orderflow_score, derivatives_score))
    if flow_score >= 0.18:
        flow_state = "ACCUMULATION"
    elif flow_score <= -0.18:
        flow_state = "DISTRIBUTION"
    else:
        flow_state = "MIXED"

    if contradictions and abs(score) < 0.30:
        decision = "BLOCK"
    confidence = clamp(
        abs(score) * 0.70 + coverage * 0.30,
        0.0,
        1.0,
    )

    unavailable = {
        name: item.status
        for name, item in items.items() if not item.active
    }
    unavailable["institutional"] = "NO_HISTORY"

    return IntelligenceResult(
        symbol=symbol,
        as_of_unix=as_of,
        decision=decision,
        market_state=state,
        flow_state=flow_state,
        score=round(score, 6),
        confidence=round(confidence, 6),
        data_coverage=round(coverage, 6),
        contradictions=contradictions,
        components={name: asdict(item) for name, item in items.items()},
        unavailable=unavailable,
    )


def main() -> None:
    result = analyze_market_intelligence()
    print("ORACLE X — MARKET INTELLIGENCE", flush=True)
    print(json.dumps(asdict(result), ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
