from __future__ import annotations

from dataclasses import dataclass, asdict
from math import sqrt
from typing import Dict, List, Optional
from statistics import median

from database.db import connect


@dataclass
class TwinCandidate:
    timestamp: str
    timestamp_unix: int
    similarity: float
    close: float
    return_1h: Optional[float]
    return_4h: Optional[float]
    return_24h: Optional[float]

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class HistoricalTwinsResult:
    symbol: str
    timeframe: str
    reference_timestamp: str
    reference_price: float
    matches: List[TwinCandidate]
    average_1h: Optional[float]
    average_4h: Optional[float]
    average_24h: Optional[float]
    median_1h: Optional[float]
    median_4h: Optional[float]
    median_24h: Optional[float]
    up_probability_1h: Optional[float]
    up_probability_4h: Optional[float]
    up_probability_24h: Optional[float]
    weighted_1h: Optional[float]
    weighted_4h: Optional[float]
    weighted_24h: Optional[float]
    historical_edge: float

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "reference_timestamp": self.reference_timestamp,
            "reference_price": self.reference_price,
            "matches": [m.to_dict() for m in self.matches],
            "average_1h": self.average_1h,
            "average_4h": self.average_4h,
            "average_24h": self.average_24h,
            "median_1h": self.median_1h,
            "median_4h": self.median_4h,
            "median_24h": self.median_24h,
            "up_probability_1h": self.up_probability_1h,
            "up_probability_4h": self.up_probability_4h,
            "up_probability_24h": self.up_probability_24h,
            "weighted_1h": self.weighted_1h,
            "weighted_4h": self.weighted_4h,
            "weighted_24h": self.weighted_24h,
            "historical_edge": self.historical_edge,
        }


def pct_change(current: float, previous: float) -> float:
    if previous == 0:
        return 0.0

    return ((current / previous) - 1.0) * 100.0


def fetch_rows(
    symbol: str,
    timeframe: str,
) -> List:
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
            ORDER BY timestamp_unix ASC
            """,
            (symbol, timeframe),
        ).fetchall()

    finally:
        con.close()

    return rows


def build_feature_vector(
    rows: List,
    index: int,
    lookback: int = 12,
) -> Optional[List[float]]:
    if index < lookback:
        return None

    closes = [
        float(rows[i]["close"])
        for i in range(index - lookback, index + 1)
    ]

    highs = [
        float(rows[i]["high"])
        for i in range(index - lookback, index + 1)
    ]

    lows = [
        float(rows[i]["low"])
        for i in range(index - lookback, index + 1)
    ]

    current = closes[-1]

    if current == 0:
        return None

    short_return = pct_change(closes[-1], closes[-4])
    medium_return = pct_change(closes[-1], closes[-7])
    long_return = pct_change(closes[-1], closes[0])

    ranges = [
        (highs[i] - lows[i]) / closes[i] * 100.0
        if closes[i] != 0
        else 0.0
        for i in range(len(closes))
    ]

    avg_range = sum(ranges) / len(ranges)

    recent_range = (
        max(highs[-4:]) - min(lows[-4:])
    ) / current * 100.0

    return [
        short_return,
        medium_return,
        long_return,
        avg_range,
        recent_range,
    ]


def euclidean_distance(
    a: List[float],
    b: List[float],
) -> float:
    return sqrt(
        sum((x - y) ** 2 for x, y in zip(a, b))
    )


def similarity_from_distance(distance: float) -> float:
    return 1.0 / (1.0 + distance)


def future_return(
    rows: List,
    index: int,
    steps: int,
) -> Optional[float]:
    future_index = index + steps

    if future_index >= len(rows):
        return None

    current = float(rows[index]["close"])
    future = float(rows[future_index]["close"])

    return pct_change(future, current)


def analyze_historical_twins(
    symbol: str = "BTC",
    timeframe: str = "15m",
    top_n: int = 10,
    lookback: int = 12,
    minimum_separation: int = 24,
) -> HistoricalTwinsResult:
    rows = fetch_rows(symbol, timeframe)

    if len(rows) < 100:
        raise ValueError("Недостатньо історичних даних")

    reference_index = len(rows) - 1

    reference_vector = build_feature_vector(
        rows,
        reference_index,
        lookback,
    )

    if reference_vector is None:
        raise ValueError("Не вдалося побудувати reference vector")

    candidates = []

    max_candidate_index = (
        reference_index - minimum_separation
    )

    for i in range(lookback, max_candidate_index):
        vector = build_feature_vector(
            rows,
            i,
            lookback,
        )

        if vector is None:
            continue

        distance = euclidean_distance(
            reference_vector,
            vector,
        )

        similarity = similarity_from_distance(distance)

        candidates.append(
            TwinCandidate(
                timestamp=rows[i]["timestamp"],
                timestamp_unix=int(
                    rows[i]["timestamp_unix"]
                ),
                similarity=round(similarity, 6),
                close=round(float(rows[i]["close"]), 2),
                return_1h=(
                    round(future_return(rows, i, 4), 4)
                    if future_return(rows, i, 4) is not None
                    else None
                ),
                return_4h=(
                    round(future_return(rows, i, 16), 4)
                    if future_return(rows, i, 16) is not None
                    else None
                ),
                return_24h=(
                    round(future_return(rows, i, 96), 4)
                    if future_return(rows, i, 96) is not None
                    else None
                ),
            )
        )

    candidates.sort(
        key=lambda x: x.similarity,
        reverse=True,
    )

    matches = candidates[:top_n]

    def average(values):
        clean = [v for v in values if v is not None]

        if not clean:
            return None

        return round(sum(clean) / len(clean), 4)

    def median_value(values):
        clean = [v for v in values if v is not None]

        if not clean:
            return None

        return round(median(clean), 4)

    def up_probability(values):
        clean = [v for v in values if v is not None]

        if not clean:
            return None

        positive = sum(1 for value in clean if value > 0)
        return round(positive / len(clean) * 100.0, 2)

    def weighted_outcome(matches, attribute):
        usable = [
            match for match in matches
            if getattr(match, attribute) is not None
        ]

        if not usable:
            return None

        total_weight = sum(match.similarity for match in usable)

        if total_weight <= 0:
            return None

        value = sum(
            getattr(match, attribute) * match.similarity
            for match in usable
        ) / total_weight

        return round(value, 4)

    returns_1h = [m.return_1h for m in matches]
    returns_4h = [m.return_4h for m in matches]
    returns_24h = [m.return_24h for m in matches]

    weighted_1h = weighted_outcome(matches, "return_1h")
    weighted_4h = weighted_outcome(matches, "return_4h")
    weighted_24h = weighted_outcome(matches, "return_24h")

    up_1h = up_probability(returns_1h)
    up_4h = up_probability(returns_4h)
    up_24h = up_probability(returns_24h)

    edge_components = []

    if up_1h is not None:
        edge_components.append(abs(up_1h - 50.0) * 0.20)

    if up_4h is not None:
        edge_components.append(abs(up_4h - 50.0) * 0.30)

    if up_24h is not None:
        edge_components.append(abs(up_24h - 50.0) * 0.50)

    historical_edge = min(
        100.0,
        sum(edge_components),
    )

    return HistoricalTwinsResult(
        symbol=symbol,
        timeframe=timeframe,
        reference_timestamp=rows[-1]["timestamp"],
        reference_price=round(
            float(rows[-1]["close"]),
            2,
        ),
        matches=matches,
        average_1h=average(returns_1h),
        average_4h=average(returns_4h),
        average_24h=average(returns_24h),
        median_1h=median_value(returns_1h),
        median_4h=median_value(returns_4h),
        median_24h=median_value(returns_24h),
        up_probability_1h=up_1h,
        up_probability_4h=up_4h,
        up_probability_24h=up_24h,
        weighted_1h=weighted_1h,
        weighted_4h=weighted_4h,
        weighted_24h=weighted_24h,
        historical_edge=round(historical_edge, 2),
    )


if __name__ == "__main__":
    result = analyze_historical_twins(
        symbol="BTC",
        timeframe="15m",
        top_n=10,
    )

    print()
    print("ORACLE X — HISTORICAL TWINS")
    print("=" * 70)
    print(f"Symbol:       {result.symbol}")
    print(f"Timeframe:    {result.timeframe}")
    print(f"Reference:    {result.reference_timestamp}")
    print(f"Price:        {result.reference_price}")
    print()

    for i, match in enumerate(result.matches, start=1):
        print(
            f"{i:02d}. "
            f"{match.timestamp} | "
            f"sim={match.similarity:.4f} | "
            f"1h={match.return_1h}% | "
            f"4h={match.return_4h}% | "
            f"24h={match.return_24h}%"
        )

    print()
    print(f"Average 1h:       {result.average_1h}%")
    print(f"Average 4h:       {result.average_4h}%")
    print(f"Average 24h:      {result.average_24h}%")
    print()
    print(f"Median 1h:        {result.median_1h}%")
    print(f"Median 4h:        {result.median_4h}%")
    print(f"Median 24h:       {result.median_24h}%")
    print()
    print(f"Up probability 1h:  {result.up_probability_1h}%")
    print(f"Up probability 4h:  {result.up_probability_4h}%")
    print(f"Up probability 24h: {result.up_probability_24h}%")
    print()
    print(f"Weighted 1h:      {result.weighted_1h}%")
    print(f"Weighted 4h:      {result.weighted_4h}%")
    print(f"Weighted 24h:     {result.weighted_24h}%")
    print()
    print(f"Historical Edge:  {result.historical_edge}%")
