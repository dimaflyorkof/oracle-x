from datetime import datetime, timezone

from evolution.backtest import run_backtest


def ts(value: str) -> int:
    return int(
        datetime.strptime(
            value,
            "%Y-%m-%d",
        ).replace(
            tzinfo=timezone.utc
        ).timestamp()
    )


WINDOWS = [
    ("EARLY", "2026-03-01", "2026-05-31"),
    ("LATE",  "2026-06-01", "2026-08-26"),
]


MODELS = [
    ("v1.0", 1.5, 2.0),
    ("v1.1", 1.7, 1.5),
]


def main():
    print()
    print("ORACLE X — WALK-FORWARD CHECK")
    print("=" * 92)

    for window_name, start_date, end_date in WINDOWS:
        print()
        print(
            f"{window_name}: "
            f"{start_date} -> {end_date}"
        )
        print("-" * 92)

        for version, stop_atr, tp_r in MODELS:
            result = run_backtest(
                symbol="BTC",
                stop_atr=stop_atr,
                tp_r=tp_r,
                start_ts=ts(start_date),
                end_ts=ts(end_date) + 86399,
            )

            print(
                f"{version:>5} | "
                f"STOP {stop_atr:.1f} | "
                f"TP {tp_r:.1f}R | "
                f"Trades {result.trades:>4} | "
                f"WR {result.win_rate:>6.2f}% | "
                f"PF {result.profit_factor:>6.4f} | "
                f"AvgR {result.average_r:>7.4f} | "
                f"Total {result.total_r:>8.2f}R | "
                f"DD {result.max_drawdown_r:>6.2f}R"
            )


if __name__ == "__main__":
    main()
