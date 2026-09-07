from __future__ import annotations

import json
import time
from dataclasses import asdict

from core.global_intelligence_v2_1 import (
    SERIES,
    VERSION,
    analyze_global_intelligence_v2_1,
    score_macro_vintages,
)
from database.db import connect


def main() -> None:
    now = int(time.time())
    current = score_macro_vintages(now)
    result = analyze_global_intelligence_v2_1(as_of_ts=now)
    con = connect()
    try:
        invalid_contract = int(con.execute(
            "SELECT COUNT(*) FROM global_macro_vintages WHERE source='alfred_vintages' "
            "AND available_at_unix < observation_timestamp_unix"
        ).fetchone()[0])
        latest_available = con.execute(
            "SELECT MAX(available_at_unix) AS value FROM global_macro_vintages "
            "WHERE source='alfred_vintages' AND available_at_unix <= ?",
            (now,),
        ).fetchone()["value"]
    finally:
        con.close()
    failures = []
    visibility_violations = sum(
        1
        for detail in current.details.get("series", {}).values()
        if detail.get("latest_available_unix") is not None
        and int(detail["latest_available_unix"]) > now
    )
    if VERSION != "GLOBAL-INTELLIGENCE-V2.1":
        failures.append("wrong version")
    if invalid_contract:
        failures.append(f"reversed macro rows: {invalid_contract}")
    if visibility_violations:
        failures.append("future rows became visible")
    if result.policy.get("trading_authority") is not False:
        failures.append("trading authority is not disabled")
    if result.policy.get("historical_backtest_authority") is not False:
        failures.append("historical backtest authority is not disabled")
    if current.active_series > len(SERIES):
        failures.append("active series exceeds fixed universe")
    report = {
        "status": "PASSED" if not failures else "FAILED",
        "version": VERSION,
        "mode": "FORWARD_SHADOW_ONLY",
        "trading_authority": False,
        "historical_backtest_authority": False,
        "visible_observations": current.observations,
        "active_series": current.active_series,
        "latest_visible_available_unix": latest_available,
        "visibility_violations": visibility_violations,
        "macro": asdict(current),
        "decision": result.decision,
        "score": result.score,
        "coverage": result.data_coverage,
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
