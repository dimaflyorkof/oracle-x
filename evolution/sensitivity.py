from evolution.backtest import run_backtest

STOP_VALUES = [1.3, 1.5, 1.7]
TP_VALUES = [1.5, 2.0, 2.5, 3.0]


def main():
    results = []

    print()
    print("ORACLE X — STOP/TP SENSITIVITY")
    print("=" * 86)

    for stop_atr in STOP_VALUES:
        for tp_r in TP_VALUES:
            print(
                f"Testing STOP {stop_atr:.1f} ATR / TP {tp_r:.1f}R ..."
            )

            result = run_backtest(
                symbol="BTC",
                stop_atr=stop_atr,
                tp_r=tp_r,
            )

            results.append(
                (
                    stop_atr,
                    tp_r,
                    result.trades,
                    result.win_rate,
                    result.total_r,
                    result.average_r,
                    result.profit_factor,
                    result.max_drawdown_r,
                )
            )

    print()
    print("RESULTS")
    print("=" * 86)
    print(
        f"{'STOP':>6} {'TP':>6} {'TRADES':>8} {'WR%':>8} "
        f"{'TOTAL R':>10} {'AVG R':>10} {'PF':>8} {'MAX DD':>10}"
    )
    print("-" * 86)

    for row in results:
        stop_atr, tp_r, trades, wr, total_r, avg_r, pf, dd = row

        print(
            f"{stop_atr:>6.1f} "
            f"{tp_r:>6.1f} "
            f"{trades:>8} "
            f"{wr:>8.2f} "
            f"{total_r:>10.2f} "
            f"{avg_r:>10.4f} "
            f"{pf:>8.4f} "
            f"{dd:>9.2f}R"
        )


if __name__ == "__main__":
    main()
