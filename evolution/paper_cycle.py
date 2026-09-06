from __future__ import annotations

from evolution.paper_signal_runner import run_paper_signal
from evolution.paper_execution_worker import run_once as run_execution
from evolution.paper_position_monitor import run_once as run_monitor


def print_result(title: str, result) -> None:
    print()
    print(title)
    print("-" * len(title))

    if isinstance(result, list):
        if not result:
            print("no actions")
            return

        for i, item in enumerate(result, 1):
            print(f"[{i}]")
            for key, value in item.items():
                print(f"{key}: {value}")
            if i != len(result):
                print()
        return

    if isinstance(result, dict):
        for key, value in result.items():
            print(f"{key}: {value}")
        return

    print(result)


def run_cycle() -> None:
    print()
    print("ORACLE X — PAPER CYCLE")
    print("=" * 70)

    # 1. Existing open positions are monitored first.
    monitor_result = run_monitor()
    print_result(
        "1. POSITION MONITOR",
        monitor_result,
    )

    # 2. Execute any due pending entries.
    execution_result = run_execution()
    print_result(
        "2. PENDING EXECUTION",
        execution_result,
    )

    # 3. Only after lifecycle checks,
    #    evaluate whether a new signal may be queued.
    try:
        signal_result = run_paper_signal()
    except Exception as exc:
        signal_result = {
            "action": "ERROR",
            "error": str(exc),
        }

    print_result(
        "3. SIGNAL ENGINE",
        signal_result,
    )


if __name__ == "__main__":
    run_cycle()
