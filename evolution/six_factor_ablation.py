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
from learning.feature_weights import get_weights


MIN_SCORE = 25.0
MIN_AGREEMENT = 60.0

STOP_ATR = 1.7
TP_R = 3.0
MAX_BARS = 32
COOLDOWN_BARS = 1

LIQ_WINDOW_MINUTES = 15

COSTS = [
    ("ZERO", 0.0, 0.0),
    ("RT8", 3.0, 1.0),
]

VARIANTS = {
    "FULL": {
        "orderflow": True,
        "derivatives": True,
        "liquidations": True,
    },
    "NO_LIQ": {
        "orderflow": True,
        "derivatives": True,
        "liquidations": False,
    },
    "NO_OF": {
        "orderflow": False,
        "derivatives": True,
        "liquidations": True,
    },
    "NO_DER": {
        "orderflow": True,
        "derivatives": False,
        "liquidations": True,
    },
    "CORE_ONLY": {
        "orderflow": False,
        "derivatives": False,
        "liquidations": False,
    },
}


def recalc_score(point, weights, flags):
    raw = (
        point.regime_component
        * weights["regime"]

        + point.structure_component
        * weights["structure"]

        + point.momentum_component
        * weights["momentum"]
    )

    if flags["orderflow"]:
        raw += (
            point.orderflow_component
            * weights["orderflow"]
        )

    if flags["derivatives"]:
        raw += (
            point.derivatives_component
            * weights["derivatives"]
        )

    if flags["liquidations"]:
        raw += (
            point.liquidations_component
            * weights["liquidations"]
        )

    return raw * 100.0


def evaluate(
    replay,
    rows_15m,
    index_15m,
    weights,
    flags,
    fee_bps,
    slippage_bps,
):
    trades = []
    next_allowed_index = 0

    raw_signals = 0

    for point in replay:
        score = recalc_score(
            point,
            weights,
            flags,
        )

        if point.agreement < MIN_AGREEMENT:
            continue

        if score >= MIN_SCORE:
            direction = "LONG"
        elif score <= -MIN_SCORE:
            direction = "SHORT"
        else:
            continue

        raw_signals += 1

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

    count = len(trades)

    wins = sum(
        1
        for t in trades
        if t.result_r > 0
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

    return {
        "signals": raw_signals,
        "trades": count,
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
        "ORACLE X — SIX-FACTOR ABLATION TEST"
    )
    print("=" * 115)

    replay = run_replay(
        symbol="BTC",
        liquidation_window_seconds=(
            LIQ_WINDOW_MINUTES * 60
        ),
    )

    rows_15m = load_rows(
        "BTC",
        "15m",
    )

    index_15m = build_timestamp_index(
        rows_15m
    )

    weights = get_weights("BTC")

    print(
        f"Replay points: {len(replay)} | "
        f"Liquidation window: {LIQ_WINDOW_MINUTES}m"
    )

    for variant, flags in VARIANTS.items():
        print()
        print(variant)
        print("-" * 115)

        for cost_label, fee, slip in COSTS:
            r = evaluate(
                replay,
                rows_15m,
                index_15m,
                weights,
                flags,
                fee,
                slip,
            )

            print(
                f"{cost_label:>5} | "
                f"Signals {r['signals']:>4} | "
                f"Trades {r['trades']:>3} | "
                f"WR {r['wr']:>6.2f}% | "
                f"Total {r['total_r']:>8.2f}R | "
                f"AvgR {r['avg_r']:>7.4f} | "
                f"PF {r['pf']:>7.4f} | "
                f"DD {r['dd']:>7.2f}R"
            )


if __name__ == "__main__":
    main()
