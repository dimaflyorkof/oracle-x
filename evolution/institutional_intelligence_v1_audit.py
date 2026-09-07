from __future__ import annotations

import json
import time
from dataclasses import asdict

from core.institutional_intelligence_v1 import VERSION, analyze_institutional_intelligence
from database.db import connect


def main() -> None:
    now = int(time.time())
    result = analyze_institutional_intelligence(now)
    con = connect()
    try:
        reversed_etf = int(con.execute(
            "SELECT COUNT(*) FROM institutional_flow_observations_v1 "
            "WHERE available_at_unix < reference_timestamp_unix"
        ).fetchone()[0])
        source = con.execute(
            "SELECT trading_authority, historical_research_authority, state "
            "FROM global_source_registry WHERE source='blackrock_ibit_holdings'"
        ).fetchone()
        rows = int(con.execute(
            "SELECT COUNT(*) FROM institutional_flow_observations_v1"
        ).fetchone()[0])
    finally:
        con.close()
    failures = []
    if VERSION != "INSTITUTIONAL-INTELLIGENCE-V1":
        failures.append("wrong version")
    if reversed_etf:
        failures.append(f"reversed ETF rows: {reversed_etf}")
    if not source or int(source["trading_authority"]) or int(source["historical_research_authority"]):
        failures.append("issuer source authority is not locked")
    if result.policy.get("trading_authority") is not False:
        failures.append("trading authority enabled")
    report = {
        "status": "PASSED" if not failures else "FAILED",
        "version": VERSION,
        "mode": "SHADOW_CONTEXT_ONLY",
        "institutional_rows": rows,
        "reversed_etf_rows": reversed_etf,
        "result": asdict(result),
        "failures": failures,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
