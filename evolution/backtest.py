from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional
from bisect import bisect_right

from database.db import connect

from core.regime import (
    Candle as RegimeCandle,
    timeframe_regime,
    TIMEFRAME_WEIGHTS,
)
from core.structure import (
    Candle as StructureCandle,
    detect_swings,
    split_swings,
    label_last_high,
    label_last_low,
    detect_break_of_structure,
    classify_structure,
)
from core.momentum import (
    Candle as MomentumCandle,
    rsi,
    roc,
    impulse_ratio,
)


@dataclass
class BacktestTrade:
    timestamp: str
    timestamp_unix: int
    direction: str
    entry: float
    stop: float
    tp: float
    result_r: float
    exit_timestamp: Optional[str]
    exit_index: int


@dataclass
class BacktestResult:
    symbol: str
    trades: int
    wins: int
    losses: int
    win_rate: float
    total_r: float
    average_r: float
    profit_factor: float
    max_drawdown_r: float


def load_rows(symbol: str, timeframe: str) -> List:
    con = connect()

    try:
        rows = con.execute(
            """
            SELECT timestamp, timestamp_unix,
                   open, high, low, close, volume
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


def build_timestamp_index(rows: List) -> List[int]:
    return [
        int(row["timestamp_unix"])
        for row in rows
    ]


def rows_until(
    rows: List,
    timestamps: List[int],
    ts: int,
    limit: int = 120,
) -> List:
    end = bisect_right(timestamps, ts)
    start = max(0, end - limit)

    return rows[start:end]


def regime_candles(rows: List) -> List[RegimeCandle]:
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


def structure_score(rows: List) -> float:
    candles = [
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

    swings = detect_swings(candles)
    highs, lows = split_swings(swings)

    high_label = label_last_high(highs)
    low_label = label_last_low(lows)
    bos = detect_break_of_structure(candles, highs, lows)

    structure, confidence = classify_structure(
        high_label,
        low_label,
        bos,
    )

    if structure == "BULLISH":
        return confidence / 100.0

    if structure == "BEARISH":
        return -(confidence / 100.0)

    return 0.0


def momentum_score(rows: List) -> float:
    candles = [
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

    closes = [c.close for c in candles]

    value_rsi = rsi(closes, 14)
    if value_rsi is None:
        return 0.0

    r5 = roc(closes, 5)
    r10 = roc(closes, 10)
    impulse = impulse_ratio(candles, 10)

    score = 0.0

    if value_rsi >= 60:
        score += 1
    elif value_rsi <= 40:
        score -= 1

    if r5 > 0:
        score += 1
    elif r5 < 0:
        score -= 1

    if r10 > 0:
        score += 1
    elif r10 < 0:
        score -= 1

    if impulse >= 0.55:
        if r5 > 0:
            score += 0.5
        elif r5 < 0:
            score -= 0.5

    return max(-1.0, min(1.0, score / 3.5))


def historical_score(
    data: Dict[str, List],
    timestamp_index: Dict[str, List[int]],
    ts: int,
) -> tuple[float, float]:
    regimes = {}
    structures = {}
    momentums = {}

    for tf in ("15m", "1h", "4h"):
        rows = rows_until(
            data[tf],
            timestamp_index[tf],
            ts,
            120,
        )

        if len(rows) < 60:
            return 0.0, 0.0

        regime = timeframe_regime(
            tf,
            regime_candles(rows),
        )

        regimes[tf] = regime
        structures[tf] = structure_score(rows)
        momentums[tf] = momentum_score(rows)

    weighted_regime = sum(
        regimes[tf].direction_score
        * TIMEFRAME_WEIGHTS[tf]
        for tf in TIMEFRAME_WEIGHTS
    )

    regime_component = max(
        -1.0,
        min(1.0, weighted_regime / 4.0),
    )

    structure_component = sum(
        structures[tf] * TIMEFRAME_WEIGHTS[tf]
        for tf in TIMEFRAME_WEIGHTS
    )

    momentum_component = sum(
        momentums[tf] * TIMEFRAME_WEIGHTS[tf]
        for tf in TIMEFRAME_WEIGHTS
    )

    score = (
        regime_component * 0.40
        + structure_component * 0.35
        + momentum_component * 0.25
    ) * 100.0

    agreement = max(
        sum(
            TIMEFRAME_WEIGHTS[tf]
            for tf in TIMEFRAME_WEIGHTS
            if regimes[tf].regime == regime_name
        )
        for regime_name in ("TREND_UP", "TREND_DOWN", "RANGE")
    ) * 100.0

    return score, agreement


def simulate_trade(
    rows_15m: List,
    start_index: int,
    direction: str,
    atr_value: float,
    max_bars: int = 32,
) -> Optional[BacktestTrade]:
    entry_row = rows_15m[start_index]
    entry = float(entry_row["close"])

    distance = atr_value * 1.5

    if distance <= 0:
        return None

    if direction == "LONG":
        stop = entry - distance
        tp = entry + distance * 2.0
    else:
        stop = entry + distance
        tp = entry - distance * 2.0

    end = min(
        len(rows_15m),
        start_index + 1 + max_bars,
    )

    for i in range(start_index + 1, end):
        row = rows_15m[i]

        high = float(row["high"])
        low = float(row["low"])

        if direction == "LONG":
            stop_hit = low <= stop
            tp_hit = high >= tp
        else:
            stop_hit = high >= stop
            tp_hit = low <= tp

        # Conservative intrabar assumption.
        if stop_hit:
            return BacktestTrade(
                timestamp=entry_row["timestamp"],
                timestamp_unix=int(entry_row["timestamp_unix"]),
                direction=direction,
                entry=entry,
                stop=stop,
                tp=tp,
                result_r=-1.0,
                exit_timestamp=row["timestamp"],
                exit_index=i,
            )

        if tp_hit:
            return BacktestTrade(
                timestamp=entry_row["timestamp"],
                timestamp_unix=int(entry_row["timestamp_unix"]),
                direction=direction,
                entry=entry,
                stop=stop,
                tp=tp,
                result_r=2.0,
                exit_timestamp=row["timestamp"],
                exit_index=i,
            )

    if end <= start_index + 1:
        return None

    exit_index = end - 1
    exit_row = rows_15m[exit_index]
    exit_price = float(exit_row["close"])

    risk = abs(entry - stop)

    if risk <= 0:
        return None

    if direction == "LONG":
        result_r = (exit_price - entry) / risk
    else:
        result_r = (entry - exit_price) / risk

    return BacktestTrade(
        timestamp=entry_row["timestamp"],
        timestamp_unix=int(entry_row["timestamp_unix"]),
        direction=direction,
        entry=entry,
        stop=stop,
        tp=tp,
        result_r=result_r,
        exit_timestamp=exit_row["timestamp"],
        exit_index=exit_index,
    )


def run_backtest(
    symbol: str = "BTC",
    min_score: float = 25.0,
    min_agreement: float = 60.0,
) -> BacktestResult:
    data = {
        tf: load_rows(symbol, tf)
        for tf in ("15m", "1h", "4h")
    }

    timestamp_index = {
        tf: build_timestamp_index(rows)
        for tf, rows in data.items()
    }

    rows_15m = data["15m"]

    trades: List[BacktestTrade] = []

    # 120 candles needed for indicators.
    # Only one position may be open at a time.
    cooldown_bars = 1
    i = 120

    while i < len(rows_15m) - 32:
        row = rows_15m[i]
        ts = int(row["timestamp_unix"])

        score, agreement = historical_score(
            data,
            timestamp_index,
            ts,
        )

        if agreement < min_agreement:
            i += 1
            continue

        if score >= min_score:
            direction = "LONG"
        elif score <= -min_score:
            direction = "SHORT"
        else:
            i += 1
            continue

        regime_rows = rows_until(
            data["15m"],
            timestamp_index["15m"],
            ts,
            120,
        )

        tf_result = timeframe_regime(
            "15m",
            regime_candles(regime_rows),
        )

        trade = simulate_trade(
            rows_15m,
            i,
            direction,
            tf_result.atr14,
        )

        if trade is not None:
            trades.append(trade)
            i = trade.exit_index + 1 + cooldown_bars
        else:
            i += 1

    wins = sum(1 for t in trades if t.result_r > 0)
    losses = sum(1 for t in trades if t.result_r < 0)

    total_r = sum(t.result_r for t in trades)

    gross_profit = sum(
        t.result_r for t in trades if t.result_r > 0
    )

    gross_loss = abs(
        sum(t.result_r for t in trades if t.result_r < 0)
    )

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else gross_profit
    )

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0

    for trade in trades:
        equity += trade.result_r
        peak = max(peak, equity)
        max_drawdown = max(
            max_drawdown,
            peak - equity,
        )

    count = len(trades)

    return BacktestResult(
        symbol=symbol,
        trades=count,
        wins=wins,
        losses=losses,
        win_rate=round(
            wins / count * 100.0 if count else 0.0,
            2,
        ),
        total_r=round(total_r, 2),
        average_r=round(
            total_r / count if count else 0.0,
            4,
        ),
        profit_factor=round(profit_factor, 4),
        max_drawdown_r=round(max_drawdown, 2),
    )


if __name__ == "__main__":
    result = run_backtest("BTC")

    print()
    print("ORACLE X — HISTORICAL BACKTEST")
    print("=" * 60)
    print(f"Symbol:       {result.symbol}")
    print(f"Trades:       {result.trades}")
    print(f"Wins:         {result.wins}")
    print(f"Losses:       {result.losses}")
    print(f"Win rate:     {result.win_rate}%")
    print(f"Total R:      {result.total_r}")
    print(f"Average R:    {result.average_r}")
    print(f"Profit factor:{result.profit_factor}")
    print(f"Max DD:       {result.max_drawdown_r}R")
