from evolution.backtest import run_backtest


MODELS = [
    ("v1.0", 1.5, 2.0),
    ("C1",   1.5, 3.0),
    ("C2",   1.7, 3.0),
]

COST_SCENARIOS = [
    (2.0, 1.0),  # 6 bps round-trip
    (3.0, 1.0),  # 8 bps
    (4.0, 1.0),  # 10 bps
    (4.0, 2.0),  # 12 bps
]


def main():
    print()
    print("ORACLE X — MODEL COST SENSITIVITY")
    print("=" * 108)

    for model, stop_atr, tp_r in MODELS:
        print()
        print(
            f"{model} | STOP {stop_atr:.1f} ATR | TP {tp_r:.1f}R"
        )
        print("-" * 108)

        print(
            f"{'FEE':>6} {'SLIP':>6} {'RT BPS':>8} "
            f"{'TRADES':>8} {'TOTAL R':>10} {'AVG R':>10} "
            f"{'PF':>8} {'MAX DD':>10} {'COST R':>10}"
        )

        for fee_bps, slippage_bps in COST_SCENARIOS:
            r = run_backtest(
                symbol="BTC",
                stop_atr=stop_atr,
                tp_r=tp_r,
                fee_bps=fee_bps,
                slippage_bps=slippage_bps,
            )

            rt_bps = 2.0 * (
                fee_bps + slippage_bps
            )

            print(
                f"{fee_bps:>6.1f} "
                f"{slippage_bps:>6.1f} "
                f"{rt_bps:>8.1f} "
                f"{r.trades:>8} "
                f"{r.total_r:>10.2f} "
                f"{r.average_r:>10.4f} "
                f"{r.profit_factor:>8.4f} "
                f"{r.max_drawdown_r:>9.2f}R "
                f"{r.total_cost_r:>9.2f}R"
            )


if __name__ == "__main__":
    main()
