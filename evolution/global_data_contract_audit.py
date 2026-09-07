from __future__ import annotations

import json
from datetime import datetime, timezone

from collectors.global_data.official_global_collector import ensure_schema
from database.db import connect


EXPECTED = {
    "bls_public_data_api": "ACTIVE_OR_DEGRADED",
    "federal_reserve_monetary_rss": "ACTIVE_OR_DEGRADED",
    "alfred_vintages": "ACTIVE_OR_DEGRADED",
    "cftc_cot": "ACTIVE_OR_DEGRADED",
    "cme_positioning": "PENDING",
    "btc_etf_flows": "PENDING",
    "official_release_calendar": "ACTIVE_OR_DEGRADED",
}


def main() -> None:
    con = connect()
    try:
        ensure_schema(con)
        health = {row["source"]: dict(row) for row in con.execute("SELECT * FROM global_source_health")}
        reversed_observations = con.execute(
            "SELECT COUNT(*) FROM global_observations WHERE observed_at_unix < available_at_unix"
        ).fetchone()[0]
        null_contract = con.execute(
            """SELECT COUNT(*) FROM global_observations
               WHERE available_at_unix IS NULL OR observed_at_unix IS NULL
                  OR data_kind IS NULL OR reference_period IS NULL"""
        ).fetchone()[0]
        event_reversed = con.execute(
            "SELECT COUNT(*) FROM global_events WHERE observed_at_unix < available_at_unix"
        ).fetchone()[0]
        positioning_reversed = con.execute(
            "SELECT COUNT(*) FROM global_positioning WHERE observed_at_unix < available_at_unix"
        ).fetchone()[0]
        source_report = []
        failures = []
        for source, expectation in EXPECTED.items():
            row = health.get(source)
            status = row["status"] if row else "MISSING"
            if row is None:
                failures.append(f"missing source registry: {source}")
            elif expectation == "PENDING" and status != "PENDING":
                failures.append(f"unexpected state for {source}: {status}")
            elif expectation == "ACTIVE_OR_DEGRADED" and status not in {"ACTIVE", "DEGRADED"}:
                failures.append(f"invalid live source state for {source}: {status}")
            source_report.append({"source": source, "status": status, "records_seen": row["records_seen"] if row else 0})
        unauthorized = con.execute(
            """SELECT COUNT(*) FROM global_source_registry
               WHERE trading_authority != 0 OR historical_research_authority != 0"""
        ).fetchone()[0]
        if unauthorized:
            failures.append(f"sources with unauthorized authority: {unauthorized}")
        if reversed_observations:
            failures.append(f"observation availability reversals: {reversed_observations}")
        if event_reversed:
            failures.append(f"event availability reversals: {event_reversed}")
        if positioning_reversed:
            failures.append(f"positioning availability reversals: {positioning_reversed}")
        if null_contract:
            failures.append(f"null observation contract rows: {null_contract}")
        report = {
            "status": "PASSED" if not failures else "FAILED",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": "CONTEXT_ONLY",
            "trading_authority": False,
            "historical_backtest_authority": False,
            "observations": con.execute("SELECT COUNT(*) FROM global_observations").fetchone()[0],
            "events": con.execute("SELECT COUNT(*) FROM global_events").fetchone()[0],
            "positioning_rows": con.execute("SELECT COUNT(*) FROM global_positioning").fetchone()[0],
            "reversed_observations": reversed_observations,
            "reversed_events": event_reversed,
            "reversed_positioning": positioning_reversed,
            "null_contract_rows": null_contract,
            "sources_with_trading_or_backtest_authority": unauthorized,
            "sources": source_report,
            "failures": failures,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if failures:
            raise SystemExit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
