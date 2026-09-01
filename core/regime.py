from __future__ import annotations

from dataclasses import dataclass, asdict
from statistics import median
from typing import Dict, List, Optional

from database.db import connect


TIMEFRAME_WEIGHTS = {
    "15m": 0.20,
    "1h": 0.35,
    "4h": 0.45,
}


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
class TimeframeRegime:
    timeframe: str
    regime: str
    direction_score: float
    confidence: float
    close: float
    ema20: float
    ema50: float
    atr14: float
    atr_percent: float
    volatility: str
    slope_percent: float
    return_percent: float


@dataclass
class MarketRegime:
    symbol: str
    regime: str
    confidence: float
    direction_score: float
    volatility: str
    agreement: float
    timeframes: Dict[str, TimeframeRegime]

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "regime": self.regime,
            "confidence": self.confidence,
            "direction_score": self.direction_score,
            "volatility": self.volatility,
            "agreement": self.agreement,
            "timeframes": {
                tf: asdict(data)
                for tf, data in self.timeframes.items()
            },
        }


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
            timestamp_unix=row["timestamp_unix"],
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row["volume"] or 0.0),
        )
        for row in rows
    ]


def ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None

    multiplier = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period

    for price in values[period:]:
        value = (price - value) * multiplier + value

    return value


def true_ranges(candles: List[Candle]) -> List[float]:
    if len(candles) < 2:
        return []

    result = []

    for i in range(1, len(candles)):
        current = candles[i]
        previous_close = candles[i - 1].close

        result.append(
            max(
                current.high - current.low,
                abs(current.high - previous_close),
                abs(current.low - previous_close),
            )
        )

    return result


def atr(candles: List[Candle], period: int = 14) -> Optional[float]:
    ranges = true_ranges(candles)

    if len(ranges) < period:
        return None

    value = sum(ranges[:period]) / period

    for tr in ranges[period:]:
        value = ((value * (period - 1)) + tr) / period

    return value


def recent_atr_percentages(
    candles: List[Candle],
    period: int = 14,
    window: int = 30,
) -> List[float]:
    result = []

    start = max(period + 1, len(candles) - window)

    for i in range(start, len(candles) + 1):
        subset = candles[:i]
        value = atr(subset, period)

        if value is None:
            continue

        close = subset[-1].close

        if close > 0:
            result.append((value / close) * 100.0)

    return result


def classify_volatility(
    current_atr_percent: float,
    history: List[float],
) -> str:
    if not history:
        return "NORMAL"

    baseline = median(history)

    if baseline <= 0:
        return "NORMAL"

    ratio = current_atr_percent / baseline

    if ratio >= 1.35:
        return "HIGH"

    if ratio <= 0.70:
        return "LOW"

    return "NORMAL"


def price_slope_percent(
    closes: List[float],
    lookback: int = 10,
) -> float:
    if len(closes) < lookback + 1:
        return 0.0

    start = closes[-lookback - 1]
    end = closes[-1]

    if start == 0:
        return 0.0

    return ((end / start) - 1.0) * 100.0


def timeframe_regime(
    timeframe: str,
    candles: List[Candle],
) -> TimeframeRegime:
    if len(candles) < 60:
        raise ValueError(
            f"{timeframe}: недостатньо свічок для regime analysis "
            f"({len(candles)} < 60)"
        )

    closes = [c.close for c in candles]

    close = closes[-1]
    ema20_value = ema(closes, 20)
    ema50_value = ema(closes, 50)
    atr14_value = atr(candles, 14)

    if ema20_value is None or ema50_value is None or atr14_value is None:
        raise ValueError(f"{timeframe}: неможливо розрахувати індикатори")

    atr_percent = (atr14_value / close) * 100.0 if close else 0.0
    atr_history = recent_atr_percentages(candles)

    volatility = classify_volatility(
        atr_percent,
        atr_history[:-1] if len(atr_history) > 1 else atr_history,
    )

    slope = price_slope_percent(closes, 10)

    lookback_close = closes[-11]
    return_percent = (
        ((close / lookback_close) - 1.0) * 100.0
        if lookback_close
        else 0.0
    )

    score = 0.0

    # 1. Price relative to EMA20
    if close > ema20_value:
        score += 1.0
    elif close < ema20_value:
        score -= 1.0

    # 2. EMA20 relative to EMA50
    if ema20_value > ema50_value:
        score += 1.0
    elif ema20_value < ema50_value:
        score -= 1.0

    # 3. Short-term slope
    slope_threshold = max(atr_percent * 0.40, 0.05)

    if slope > slope_threshold:
        score += 1.0
    elif slope < -slope_threshold:
        score -= 1.0

    # 4. Recent return
    return_threshold = max(atr_percent * 0.60, 0.08)

    if return_percent > return_threshold:
        score += 1.0
    elif return_percent < -return_threshold:
        score -= 1.0

    if score >= 2.0:
        regime = "TREND_UP"
    elif score <= -2.0:
        regime = "TREND_DOWN"
    else:
        regime = "RANGE"

    confidence = min(100.0, (abs(score) / 4.0) * 100.0)

    return TimeframeRegime(
        timeframe=timeframe,
        regime=regime,
        direction_score=round(score, 3),
        confidence=round(confidence, 2),
        close=round(close, 2),
        ema20=round(ema20_value, 2),
        ema50=round(ema50_value, 2),
        atr14=round(atr14_value, 2),
        atr_percent=round(atr_percent, 4),
        volatility=volatility,
        slope_percent=round(slope, 4),
        return_percent=round(return_percent, 4),
    )


def analyze_regime(symbol: str = "BTC") -> MarketRegime:
    results: Dict[str, TimeframeRegime] = {}

    for timeframe in TIMEFRAME_WEIGHTS:
        candles = fetch_candles(
            symbol=symbol,
            timeframe=timeframe,
            limit=120,
        )

        results[timeframe] = timeframe_regime(
            timeframe,
            candles,
        )

    weighted_score = sum(
        results[tf].direction_score * TIMEFRAME_WEIGHTS[tf]
        for tf in TIMEFRAME_WEIGHTS
    )

    if weighted_score >= 1.25:
        overall_regime = "TREND_UP"
    elif weighted_score <= -1.25:
        overall_regime = "TREND_DOWN"
    else:
        overall_regime = "RANGE"

    regime_weights = {
        "TREND_UP": 0.0,
        "TREND_DOWN": 0.0,
        "RANGE": 0.0,
    }

    for tf, weight in TIMEFRAME_WEIGHTS.items():
        regime_weights[results[tf].regime] += weight

    agreement = max(regime_weights.values()) * 100.0

    directional_strength = min(
        100.0,
        abs(weighted_score) / 4.0 * 100.0,
    )

    confidence = (
        directional_strength * 0.65
        + agreement * 0.35
    )

    volatility_votes = {
        "LOW": 0.0,
        "NORMAL": 0.0,
        "HIGH": 0.0,
    }

    for tf, weight in TIMEFRAME_WEIGHTS.items():
        volatility_votes[results[tf].volatility] += weight

    overall_volatility = max(
        volatility_votes,
        key=volatility_votes.get,
    )

    return MarketRegime(
        symbol=symbol,
        regime=overall_regime,
        confidence=round(confidence, 2),
        direction_score=round(weighted_score, 3),
        volatility=overall_volatility,
        agreement=round(agreement, 2),
        timeframes=results,
    )


if __name__ == "__main__":
    result = analyze_regime("BTC")

    print()
    print("ORACLE X — MARKET REGIME")
    print("=" * 50)
    print(f"Symbol:       {result.symbol}")
    print(f"Regime:       {result.regime}")
    print(f"Confidence:   {result.confidence}%")
    print(f"Direction:    {result.direction_score}")
    print(f"Volatility:   {result.volatility}")
    print(f"MTF agreement:{result.agreement}%")
    print()

    for timeframe, data in result.timeframes.items():
        print(
            f"{timeframe:>3} | "
            f"{data.regime:<10} | "
            f"score={data.direction_score:>5} | "
            f"conf={data.confidence:>6}% | "
            f"vol={data.volatility:<6} | "
            f"close={data.close}"
        )
