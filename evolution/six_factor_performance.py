from bisect import bisect_left

from core.regime import timeframe_regime
from evolution.backtest import (
    load_rows,
    build_timestamp_index,
    rows_until,
    regime_candles,
    simulate_trade,
)
from evolution.six_factor_replay import run_replay


MIN_SCORE = 25.0
MIN_AGREEMENT = 60.0

STOP_ATR = 1.7
TP_R = 3.0
MAX_BARS = 32
COOLDOWN_BARS = 1

WINDOWS = (15, 30, 60)

COSTS = [
    ("ZERO", 0.0, 0.0),
    ("RT8", 3.0, 1.0),
]


def evaluate_window(
    minutes: int,
    fee_bps: float,
    slippage_bps: float,
):
    replay = run_replay(
        symbol="BTC",
        liquidation_window_seconds=minutes * 60,
    )

    rows_15m = load_rows("BTC", "15m")
    index_15m = build_timestamp_index(rows_15m)

    trades = []

    next_allowed_index = 0

    for point in replay:
        if point.agreement < MIN_AGREEMENT:
            continue

        if point.score >= MIN_SCORE:
            direction = "LONG"
        elif point.score <= -MIN_SCORE:
            direction = "SHORT"
        else:
            continue

        i = bisect_left(
            index_15m,
            point.timestamp_unix,
        )

        if (
            i >= len(rows_15m)
            or index_15m[i] != point.timestamp_unix
        ):
            continue

        if i < next_allowed_index:
            continue

        regime_rows = rows_until(
            rows_15m,
            index_15m,
            point.timestamp_unix,
            120,
        )

        if len(regime_rows) < 60:
            continue

        tf_result = timeframe_regime(
            "15m",
            regime_candles(regime_rows),
        )

        trade = simulate_trade(
            rows_15m,
            i,
            direction,
            tf_result.atr14,
            stop_atr=STOP_ATR,
            tp_r=TP_R,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
            max_bars=MAX_BARS,
        )

        if trade is None:
            continue

        trades.append(trade)

        next_allowed_index = (
            trade.exit_index
            + 1
            + COOLDOWN_BARS
        )

    wins = sum(
        1 for t in trades
        if t.result_r > 0
    )

    losses = sum(
        1 for t in trades
        if t.result_r < 0
    )

    total_r = sum(
        t.result_r
        for t in trades
    )

    gross_profit = sum(
        t.result_r
        for t in trades
        if t.result_r > 0
    )

    gross_loss = abs(
        sum(
            t.result_r
            for t in trades
            if t.result_r < 0
        )
    )

    pf = (
        gross_profit / gross_loss
        if gross_loss > 0
        else gross_profit
    )

    equity = 0.0
    peak = 0.0
    max_dd = 0.0

    for trade in trades:
        equity += trade.result_r
        peak = max(
            peak,
            equity,
        )
        max_dd = max(
            max_dd,
            peak - equity,
        )

    count = len(trades)

    return {
        "trades": count,
        "wins": wins,
        "losses": losses,
        "wr": (
            wins / count * 100.0
            if count
            else 0.0
        ),
        "total_r": total_r,
        "avg_r": (
            total_r / count
            if count
            else 0.0
        ),
        "pf": pf,
        "dd": max_dd,
    }


def main():
    print()
    print(
        "ORACLE X — SIX-FACTOR "
        "LIQUIDATION WINDOW PERFORMANCE"
    )
    print("=" * 100)

    for minutes in WINDOWS:
        print()
        print(
            f"LIQ WINDOW = {minutes} MIN"
        )
        print("-" * 100)

        for label, fee, slip in COSTS:
            r = evaluate_window(
                minutes,
                fee,
                slip,
            )

            print(
                f"{label:>5} | "
                f"Trades {r['trades']:>4} | "
                f"WR {r['wr']:>6.2f}% | "
                f"Total {r['total_r']:>8.2f}R | "
                f"AvgR {r['avg_r']:>7.4f} | "
                f"PF {r['pf']:>7.4f} | "
                f"DD {r['dd']:>7.2f}R"
            )


if __name__ == "__main__":
    main()
