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
    exit_reason: str
    mfe_r: float
    mae_r: float
    cost_r: float


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
    tp_count: int
    stop_count: int
    time_exit_count: int
    tp_total_r: float
    stop_total_r: float
    time_exit_total_r: float
    long_count: int
    long_wins: int
    long_total_r: float
    short_count: int
    short_wins: int
    short_total_r: float
    monthly_stats: dict
    avg_mfe_r: float
    avg_mae_r: float
    winners_avg_mfe_r: float
    winners_avg_mae_r: float
    losers_avg_mfe_r: float
    losers_avg_mae_r: float
    stop_avg_mfe_r: float
    time_exit_avg_mfe_r: float
    time_exit_avg_mae_r: float
    avg_cost_r: float
    total_cost_r: float
    long_avg_cost_r: float
    short_avg_cost_r: float
    tp_avg_cost_r: float
    stop_avg_cost_r: float
    time_exit_avg_cost_r: float


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
        # ts is the open time of the 15m signal candle.
        # The decision is made only after that candle closes.
        signal_close_ts = ts + 15 * 60
        timeframe_seconds = {
            "15m": 15 * 60,
            "1h": 60 * 60,
            "4h": 4 * 60 * 60,
        }[tf]

        # Include only candles fully closed by decision time.
        last_closed_open_ts = signal_close_ts - timeframe_seconds

        rows = rows_until(
            data[tf],
            timestamp_index[tf],
            last_closed_open_ts,
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
    stop_atr: float = 1.5,
    tp_r: float = 2.0,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    max_bars: int = 32,
) -> Optional[BacktestTrade]:
    signal_row = rows_15m[start_index]

    entry_index = start_index + 1

    if entry_index >= len(rows_15m):
        return None

    entry_row = rows_15m[entry_index]
    entry = float(entry_row["open"])

    distance = atr_value * stop_atr

    if distance <= 0:
        return None

    if direction == "LONG":
        stop = entry - distance
        tp = entry + distance * tp_r
    else:
        stop = entry + distance
        tp = entry - distance * tp_r

    risk = abs(entry - stop)

    if risk <= 0:
        return None

    round_trip_bps = 2.0 * (fee_bps + slippage_bps)
    cost_price = entry * (round_trip_bps / 10000.0)
    cost_r = cost_price / risk

    mfe_r = 0.0
    mae_r = 0.0

    end = min(
        len(rows_15m),
        entry_index + max_bars,
    )

    for i in range(entry_index, end):
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
                result_r=-1.0 - cost_r,
                exit_timestamp=row["timestamp"],
                exit_index=i,
                exit_reason="STOP",
                mfe_r=mfe_r,
                mae_r=max(mae_r, 1.0),
                cost_r=cost_r,
            )

        if tp_hit:
            return BacktestTrade(
                timestamp=entry_row["timestamp"],
                timestamp_unix=int(entry_row["timestamp_unix"]),
                direction=direction,
                entry=entry,
                stop=stop,
                tp=tp,
                result_r=tp_r - cost_r,
                exit_timestamp=row["timestamp"],
                exit_index=i,
                exit_reason="TP",
                mfe_r=max(mfe_r, tp_r),
                mae_r=mae_r,
                cost_r=cost_r,
            )

        if direction == "LONG":
            favorable_r = (high - entry) / risk
            adverse_r = (entry - low) / risk
        else:
            favorable_r = (entry - low) / risk
            adverse_r = (high - entry) / risk

        mfe_r = max(mfe_r, favorable_r, 0.0)
        mae_r = max(mae_r, adverse_r, 0.0)

    if end <= start_index + 1:
        return None

    exit_index = end - 1
    exit_row = rows_15m[exit_index]
    exit_price = float(exit_row["close"])

    if direction == "LONG":
        result_r = (exit_price - entry) / risk
    else:
        result_r = (entry - exit_price) / risk

    result_r -= cost_r

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
        exit_reason="TIME_EXIT",
        mfe_r=mfe_r,
        mae_r=mae_r,
        cost_r=cost_r,
    )


def run_backtest(
    symbol: str = "BTC",
    min_score: float = 25.0,
    min_agreement: float = 60.0,
    stop_atr: float = 1.5,
    tp_r: float = 2.0,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    start_ts: int | None = None,
    end_ts: int | None = None,
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

        if start_ts is not None and ts < start_ts:
            i += 1
            continue

        if end_ts is not None and ts > end_ts:
            break

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
            stop_atr=stop_atr,
            tp_r=tp_r,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
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

    tp_trades = [t for t in trades if t.exit_reason == "TP"]
    stop_trades = [t for t in trades if t.exit_reason == "STOP"]
    time_exit_trades = [t for t in trades if t.exit_reason == "TIME_EXIT"]

    long_trades = [t for t in trades if t.direction == "LONG"]
    short_trades = [t for t in trades if t.direction == "SHORT"]

    monthly = {}

    for trade in trades:
        month = trade.timestamp[:7]

        if month not in monthly:
            monthly[month] = {
                "trades": 0,
                "wins": 0,
                "total_r": 0.0,
            }

        monthly[month]["trades"] += 1
        monthly[month]["total_r"] += trade.result_r

        if trade.result_r > 0:
            monthly[month]["wins"] += 1

    winning_trades = [t for t in trades if t.result_r > 0]
    losing_trades = [t for t in trades if t.result_r < 0]

    def avg(values):
        return sum(values) / len(values) if values else 0.0

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

        tp_count=len(tp_trades),
        stop_count=len(stop_trades),
        time_exit_count=len(time_exit_trades),

        tp_total_r=round(sum(t.result_r for t in tp_trades), 2),
        stop_total_r=round(sum(t.result_r for t in stop_trades), 2),
        time_exit_total_r=round(sum(t.result_r for t in time_exit_trades), 2),

        long_count=len(long_trades),
        long_wins=sum(1 for t in long_trades if t.result_r > 0),
        long_total_r=round(sum(t.result_r for t in long_trades), 2),

        short_count=len(short_trades),
        short_wins=sum(1 for t in short_trades if t.result_r > 0),
        short_total_r=round(sum(t.result_r for t in short_trades), 2),
        monthly_stats=monthly,

        avg_mfe_r=round(avg([t.mfe_r for t in trades]), 4),
        avg_mae_r=round(avg([t.mae_r for t in trades]), 4),

        winners_avg_mfe_r=round(
            avg([t.mfe_r for t in winning_trades]), 4
        ),
        winners_avg_mae_r=round(
            avg([t.mae_r for t in winning_trades]), 4
        ),

        losers_avg_mfe_r=round(
            avg([t.mfe_r for t in losing_trades]), 4
        ),
        losers_avg_mae_r=round(
            avg([t.mae_r for t in losing_trades]), 4
        ),

        stop_avg_mfe_r=round(
            avg([t.mfe_r for t in stop_trades]), 4
        ),

        time_exit_avg_mfe_r=round(
            avg([t.mfe_r for t in time_exit_trades]), 4
        ),
        time_exit_avg_mae_r=round(
            avg([t.mae_r for t in time_exit_trades]), 4
        ),

        avg_cost_r=round(
            avg([t.cost_r for t in trades]), 4
        ),
        total_cost_r=round(
            sum(t.cost_r for t in trades), 2
        ),

        long_avg_cost_r=round(
            avg([t.cost_r for t in long_trades]), 4
        ),
        short_avg_cost_r=round(
            avg([t.cost_r for t in short_trades]), 4
        ),

        tp_avg_cost_r=round(
            avg([t.cost_r for t in tp_trades]), 4
        ),
        stop_avg_cost_r=round(
            avg([t.cost_r for t in stop_trades]), 4
        ),
        time_exit_avg_cost_r=round(
            avg([t.cost_r for t in time_exit_trades]), 4
        ),
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
    print()
    print("EXIT BREAKDOWN")
    print("-" * 60)
    print(f"TP:           {result.tp_count} | {result.tp_total_r}R")
    print(f"STOP:         {result.stop_count} | {result.stop_total_r}R")
    print(f"TIME_EXIT:    {result.time_exit_count} | {result.time_exit_total_r}R")
    print()
    print("DIRECTION BREAKDOWN")
    print("-" * 60)

    long_wr = (
        result.long_wins / result.long_count * 100.0
        if result.long_count else 0.0
    )
    short_wr = (
        result.short_wins / result.short_count * 100.0
        if result.short_count else 0.0
    )

    print(
        f"LONG:         {result.long_count} trades | "
        f"WR {long_wr:.2f}% | {result.long_total_r}R"
    )
    print(
        f"SHORT:        {result.short_count} trades | "
        f"WR {short_wr:.2f}% | {result.short_total_r}R"
    )

    print()
    print("MONTHLY BREAKDOWN")
    print("-" * 60)

    print()
    print("MFE / MAE ANALYSIS")
    print("-" * 60)
    print(
        f"ALL:          Avg MFE {result.avg_mfe_r:.4f}R | "
        f"Avg MAE {result.avg_mae_r:.4f}R"
    )
    print(
        f"WINNERS:      Avg MFE {result.winners_avg_mfe_r:.4f}R | "
        f"Avg MAE {result.winners_avg_mae_r:.4f}R"
    )
    print(
        f"LOSERS:       Avg MFE {result.losers_avg_mfe_r:.4f}R | "
        f"Avg MAE {result.losers_avg_mae_r:.4f}R"
    )
    print(
        f"STOP trades:  Avg MFE {result.stop_avg_mfe_r:.4f}R"
    )
    print(
        f"TIME_EXIT:    Avg MFE {result.time_exit_avg_mfe_r:.4f}R | "
        f"Avg MAE {result.time_exit_avg_mae_r:.4f}R"
    )

    print()
    print("COST ANALYSIS")
    print("-" * 60)
    print(
        f"ALL:          Avg cost {result.avg_cost_r:.4f}R | "
        f"Total cost {result.total_cost_r:.2f}R"
    )
    print(
        f"LONG:         Avg cost {result.long_avg_cost_r:.4f}R"
    )
    print(
        f"SHORT:        Avg cost {result.short_avg_cost_r:.4f}R"
    )
    print(
        f"TP:           Avg cost {result.tp_avg_cost_r:.4f}R"
    )
    print(
        f"STOP:         Avg cost {result.stop_avg_cost_r:.4f}R"
    )
    print(
        f"TIME_EXIT:    Avg cost {result.time_exit_avg_cost_r:.4f}R"
    )

    print()
    print("MONTHLY BREAKDOWN")
    print("-" * 60)

    for month in sorted(result.monthly_stats):
        stats = result.monthly_stats[month]

        wr = (
            stats["wins"] / stats["trades"] * 100.0
            if stats["trades"] else 0.0
        )

        print(
            f"{month}: "
            f"{stats['trades']} trades | "
            f"WR {wr:.2f}% | "
            f"{stats['total_r']:+.2f}R"
        )
