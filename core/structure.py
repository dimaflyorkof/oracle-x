from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

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
class SwingPoint:
    index: int
    timestamp: str
    timestamp_unix: int
    price: float
    kind: str


@dataclass
class StructureResult:
    symbol: str
    timeframe: str
    structure: str
    confidence: float
    last_price: float
    last_swing_high: Optional[float]
    last_swing_low: Optional[float]
    last_high_label: Optional[str]
    last_low_label: Optional[str]
    break_of_structure: Optional[str]
    change_of_character: Optional[str]
    swing_count: int

    def to_dict(self) -> Dict:
        return asdict(self)


def fetch_candles(
    symbol: str,
    timeframe: str,
    limit: int = 250,
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


def detect_swings(
    candles: List[Candle],
    left: int = 2,
    right: int = 2,
) -> List[SwingPoint]:
    swings: List[SwingPoint] = []

    if len(candles) < left + right + 1:
        return swings

    for i in range(left, len(candles) - right):
        current = candles[i]

        left_slice = candles[i - left:i]
        right_slice = candles[i + 1:i + right + 1]

        is_swing_high = all(
            current.high > c.high for c in left_slice
        ) and all(
            current.high >= c.high for c in right_slice
        )

        is_swing_low = all(
            current.low < c.low for c in left_slice
        ) and all(
            current.low <= c.low for c in right_slice
        )

        if is_swing_high:
            swings.append(
                SwingPoint(
                    index=i,
                    timestamp=current.timestamp,
                    timestamp_unix=current.timestamp_unix,
                    price=current.high,
                    kind="HIGH",
                )
            )

        if is_swing_low:
            swings.append(
                SwingPoint(
                    index=i,
                    timestamp=current.timestamp,
                    timestamp_unix=current.timestamp_unix,
                    price=current.low,
                    kind="LOW",
                )
            )

    swings.sort(key=lambda x: x.index)
    return swings


def split_swings(
    swings: List[SwingPoint],
) -> Tuple[List[SwingPoint], List[SwingPoint]]:
    highs = [s for s in swings if s.kind == "HIGH"]
    lows = [s for s in swings if s.kind == "LOW"]

    return highs, lows


def label_last_high(
    highs: List[SwingPoint],
) -> Optional[str]:
    if len(highs) < 2:
        return None

    previous = highs[-2].price
    current = highs[-1].price

    if current > previous:
        return "HH"

    if current < previous:
        return "LH"

    return "EH"


def label_last_low(
    lows: List[SwingPoint],
) -> Optional[str]:
    if len(lows) < 2:
        return None

    previous = lows[-2].price
    current = lows[-1].price

    if current > previous:
        return "HL"

    if current < previous:
        return "LL"

    return "EL"


def detect_break_of_structure(
    candles: List[Candle],
    highs: List[SwingPoint],
    lows: List[SwingPoint],
) -> Optional[str]:
    if not candles:
        return None

    last_close = candles[-1].close

    previous_high = highs[-1].price if highs else None
    previous_low = lows[-1].price if lows else None

    if previous_high is not None and last_close > previous_high:
        return "BULLISH_BOS"

    if previous_low is not None and last_close < previous_low:
        return "BEARISH_BOS"

    return None


def detect_change_of_character(
    candles: List[Candle],
    highs: List[SwingPoint],
    lows: List[SwingPoint],
    high_label: Optional[str],
    low_label: Optional[str],
) -> Optional[str]:
    if not candles:
        return None

    last_close = candles[-1].close

    if (
        high_label == "LH"
        and lows
        and last_close > highs[-1].price
    ):
        return "BULLISH_CHOCH"

    if (
        low_label == "HL"
        and highs
        and last_close < lows[-1].price
    ):
        return "BEARISH_CHOCH"

    return None


def classify_structure(
    high_label: Optional[str],
    low_label: Optional[str],
    bos: Optional[str],
) -> Tuple[str, float]:
    bullish_score = 0.0
    bearish_score = 0.0

    if high_label == "HH":
        bullish_score += 1.0
    elif high_label == "LH":
        bearish_score += 1.0

    if low_label == "HL":
        bullish_score += 1.0
    elif low_label == "LL":
        bearish_score += 1.0

    if bos == "BULLISH_BOS":
        bullish_score += 1.5

    if bos == "BEARISH_BOS":
        bearish_score += 1.5

    total = bullish_score + bearish_score

    if bullish_score >= bearish_score + 1.0:
        structure = "BULLISH"
        confidence = (
            bullish_score / total * 100.0
            if total > 0
            else 0.0
        )

    elif bearish_score >= bullish_score + 1.0:
        structure = "BEARISH"
        confidence = (
            bearish_score / total * 100.0
            if total > 0
            else 0.0
        )

    else:
        structure = "RANGE"
        confidence = 50.0 if total > 0 else 0.0

    return structure, round(confidence, 2)


def analyze_structure(
    symbol: str = "BTC",
    timeframe: str = "15m",
    limit: int = 250,
) -> StructureResult:
    candles = fetch_candles(
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
    )

    if len(candles) < 20:
        raise ValueError(
            f"{timeframe}: недостатньо свічок для structure analysis"
        )

    swings = detect_swings(candles)
    highs, lows = split_swings(swings)

    high_label = label_last_high(highs)
    low_label = label_last_low(lows)

    bos = detect_break_of_structure(
        candles,
        highs,
        lows,
    )

    choch = detect_change_of_character(
        candles,
        highs,
        lows,
        high_label,
        low_label,
    )

    structure, confidence = classify_structure(
        high_label,
        low_label,
        bos,
    )

    last_swing_high = highs[-1].price if highs else None
    last_swing_low = lows[-1].price if lows else None

    return StructureResult(
        symbol=symbol,
        timeframe=timeframe,
        structure=structure,
        confidence=confidence,
        last_price=round(candles[-1].close, 2),
        last_swing_high=(
            round(last_swing_high, 2)
            if last_swing_high is not None
            else None
        ),
        last_swing_low=(
            round(last_swing_low, 2)
            if last_swing_low is not None
            else None
        ),
        last_high_label=high_label,
        last_low_label=low_label,
        break_of_structure=bos,
        change_of_character=choch,
        swing_count=len(swings),
    )


if __name__ == "__main__":
    for tf in ("15m", "1h", "4h"):
        result = analyze_structure(
            symbol="BTC",
            timeframe=tf,
        )

        print()
        print(f"ORACLE X — STRUCTURE {tf}")
        print("=" * 50)
        print(f"Structure:       {result.structure}")
        print(f"Confidence:      {result.confidence}%")
        print(f"Last price:      {result.last_price}")
        print(f"Last swing high: {result.last_swing_high}")
        print(f"High label:      {result.last_high_label}")
        print(f"Last swing low:  {result.last_swing_low}")
        print(f"Low label:       {result.last_low_label}")
        print(f"BOS:             {result.break_of_structure}")
        print(f"CHOCH:           {result.change_of_character}")
        print(f"Swings:          {result.swing_count}")
