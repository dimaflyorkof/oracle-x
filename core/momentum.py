from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional

from database.db import connect


@dataclass
class Candle:
    timestamp: str
    timestamp_unix: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class MomentumResult:
    symbol: str
    timeframe: str
    state: str
    confidence: float
    score: float
    rsi14: float
    roc5: float
    roc10: float
    impulse_ratio: float
    acceleration: str
    divergence: Optional[str]
    last_price: float

    def to_dict(self) -> Dict:
        return asdict(self)


def fetch_candles(
    symbol: str,
    timeframe: str,
    limit: int = 120,
) -> List[Candle]:
    con = connect()

    try:
        rows = con.execute(
            """
            SELECT
                timestamp,
                timestamp_unix,
                open,
                high,
                low,
                close,
                volume
            FROM market_snapshots
            WHERE symbol = ?
              AND timeframe = ?
              AND open IS NOT NULL
              AND high IS NOT NULL
              AND low IS NOT NULL
              AND close IS NOT NULL
            ORDER BY timestamp_unix DESC
            LIMIT ?
            """,
            (symbol, timeframe, limit),
        ).fetchall()
    finally:
        con.close()

    rows = list(reversed(rows))

    return [
        Candle(
            timestamp=row["timestamp"],
            timestamp_unix=int(row["timestamp_unix"]),
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"] or 0.0),
        )
        for row in rows
    ]


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    if len(values) < period + 1:
        return None

    gains = []
    losses = []

    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = max(change, 0.0)
        loss = max(-change, 0.0)

        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def roc(values: List[float], period: int) -> float:
    if len(values) < period + 1:
        return 0.0

    previous = values[-period - 1]
    current = values[-1]

    if previous == 0:
        return 0.0

    return ((current / previous) - 1.0) * 100.0


def impulse_ratio(candles: List[Candle], lookback: int = 10) -> float:
    if len(candles) < lookback:
        return 0.0

    recent = candles[-lookback:]

    body_sum = 0.0
    range_sum = 0.0

    for candle in recent:
        body_sum += abs(candle.close - candle.open)
        range_sum += max(candle.high - candle.low, 0.0)

    if range_sum == 0:
        return 0.0

    return body_sum / range_sum


def momentum_acceleration(
    values: List[float],
) -> str:
    if len(values) < 11:
        return "STABLE"

    recent = roc(values, 5)

    older_base = values[-11]
    older_end = values[-6]

    if older_base == 0:
        return "STABLE"

    older = ((older_end / older_base) - 1.0) * 100.0

    if abs(recent) > abs(older) * 1.25:
        return "ACCELERATING"

    if abs(recent) < abs(older) * 0.75:
        return "FADING"

    return "STABLE"


def detect_rsi_divergence(
    closes: List[float],
    rsi_values: List[float],
    lookback: int = 20,
) -> Optional[str]:
    if len(closes) < lookback or len(rsi_values) < lookback:
        return None

    price_recent = closes[-lookback:]
    rsi_recent = rsi_values[-lookback:]

    half = lookback // 2

    first_prices = price_recent[:half]
    second_prices = price_recent[half:]

    first_rsi = rsi_recent[:half]
    second_rsi = rsi_recent[half:]

    if (
        max(second_prices) > max(first_prices)
        and max(second_rsi) < max(first_rsi)
    ):
        return "BEARISH_DIVERGENCE"

    if (
        min(second_prices) < min(first_prices)
        and min(second_rsi) > min(first_rsi)
    ):
        return "BULLISH_DIVERGENCE"

    return None


def build_rsi_series(
    closes: List[float],
    period: int = 14,
) -> List[float]:
    values = []

    for i in range(period + 1, len(closes) + 1):
        value = rsi(closes[:i], period)

        if value is not None:
            values.append(value)

    return values


def analyze_momentum(
    symbol: str = "BTC",
    timeframe: str = "15m",
    limit: int = 120,
) -> MomentumResult:
    candles = fetch_candles(
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
    )

    if len(candles) < 40:
        raise ValueError(
            f"{timeframe}: недостатньо свічок для momentum analysis"
        )

    closes = [c.close for c in candles]

    rsi14 = rsi(closes, 14)

    if rsi14 is None:
        raise ValueError(f"{timeframe}: RSI неможливо розрахувати")

    roc5 = roc(closes, 5)
    roc10 = roc(closes, 10)

    impulse = impulse_ratio(candles, 10)
    acceleration = momentum_acceleration(closes)

    rsi_series = build_rsi_series(closes, 14)

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

    return MomentumResult(
        symbol=symbol,
        timeframe=timeframe,
        state=state,
        confidence=round(confidence, 2),
        score=round(score, 3),
        rsi14=round(rsi14, 2),
        roc5=round(roc5, 4),
        roc10=round(roc10, 4),
        impulse_ratio=round(impulse, 4),
        acceleration=acceleration,
        divergence=divergence,
        last_price=round(closes[-1], 2),
    )


if __name__ == "__main__":
    for tf in ("15m", "1h", "4h"):
        result = analyze_momentum(
            symbol="BTC",
            timeframe=tf,
        )

        print()
        print(f"ORACLE X — MOMENTUM {tf}")
        print("=" * 50)
        print(f"State:        {result.state}")
        print(f"Confidence:   {result.confidence}%")
        print(f"Score:        {result.score}")
        print(f"RSI14:        {result.rsi14}")
        print(f"ROC5:         {result.roc5}%")
        print(f"ROC10:        {result.roc10}%")
        print(f"Impulse:      {result.impulse_ratio}")
        print(f"Acceleration: {result.acceleration}")
        print(f"Divergence:   {result.divergence}")
        print(f"Last price:   {result.last_price}")
