from evolution.backtest import run_backtest

COST_SCENARIOS = [
    (2.0, 1.0),
    (3.0, 1.0),
    (4.0, 1.0),
    (4.0, 2.0),
    (5.0, 3.0),
]

def main():
    print()
    print("ORACLE X — v1.0 COST SENSITIVITY")
    print("=" * 88)
    print(
        f"{'FEE':>6} {'SLIP':>6} {'RT BPS':>8} "
        f"{'TRADES':>8} {'TOTAL R':>10} {'AVG R':>10} "
        f"{'PF':>8} {'MAX DD':>10}"
    )
    print("-" * 88)

    for fee_bps, slippage_bps in COST_SCENARIOS:
        r = run_backtest(
            symbol="BTC",
            stop_atr=1.5,
            tp_r=2.0,
            fee_bps=fee_bps,
            slippage_bps=slippage_bps,
        )

        rt_bps = 2.0 * (fee_bps + slippage_bps)

        print(
            f"{fee_bps:>6.1f} "
            f"{slippage_bps:>6.1f} "
            f"{rt_bps:>8.1f} "
            f"{r.trades:>8} "
            f"{r.total_r:>10.2f} "
            f"{r.average_r:>10.4f} "
            f"{r.profit_factor:>8.4f} "
            f"{r.max_drawdown_r:>9.2f}R"
        )

if __name__ == "__main__":
    main()
