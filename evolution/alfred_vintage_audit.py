from __future__ import annotations

import json
from datetime import datetime, timezone

from collectors.global_data.alfred_vintage_collector import SERIES, ensure_alfred_schema
from database.db import connect


def main() -> None:
    con = connect()
    try:
        ensure_alfred_schema(con)
        failures: list[str] = []
        row = con.execute(
            """
            SELECT COUNT(*) AS rows,
                   COUNT(DISTINCT series_id) AS series,
                   MIN(observation_timestamp_unix) AS first_observation,
                   MAX(observation_timestamp_unix) AS last_observation,
                   MIN(available_at_unix) AS first_available,
                   MAX(available_at_unix) AS last_available
            FROM global_macro_vintages
            WHERE source='alfred_vintages'
            """
        ).fetchone()
        missing = con.execute(
            """
            SELECT COUNT(*) FROM global_macro_vintages
            WHERE source='alfred_vintages'
              AND (series_id IS NULL OR observation_date IS NULL
                   OR realtime_start IS NULL OR available_at_unix IS NULL
                   OR observed_at_unix IS NULL OR value IS NULL
                   OR vintage_kind IS NULL OR data_kind IS NULL OR quality IS NULL)
            """
        ).fetchone()[0]
        reversed_time = con.execute(
            """
            SELECT COUNT(*) FROM global_macro_vintages
            WHERE source='alfred_vintages'
              AND available_at_unix < observation_timestamp_unix
            """
        ).fetchone()[0]
        non_conservative = con.execute(
            """
            SELECT COUNT(*) FROM global_macro_vintages
            WHERE source='alfred_vintages'
              AND available_at_unix < CAST(strftime('%s', date(realtime_start, '+1 day')) AS INTEGER)
            """
        ).fetchone()[0]
        duplicate_groups = con.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT series_id, observation_date, realtime_start, COUNT(*) AS n
                FROM global_macro_vintages
                WHERE source='alfred_vintages'
                GROUP BY series_id, observation_date, realtime_start
                HAVING n > 1
            )
            """
        ).fetchone()[0]
        authorities = con.execute(
            """
            SELECT trading_authority, historical_research_authority, state
            FROM global_source_registry WHERE source='alfred_vintages'
            """
        ).fetchone()
        health = con.execute(
            "SELECT status, records_seen, records_changed, error FROM global_source_health WHERE source='alfred_vintages'"
        ).fetchone()
        series_rows = [
            dict(item)
            for item in con.execute(
                """
                SELECT series_id, series_name, COUNT(*) AS rows,
                       MIN(observation_date) AS first_observation,
                       MAX(observation_date) AS latest_observation,
                       MIN(realtime_start) AS first_release_date,
                       MAX(realtime_start) AS latest_release_date
                FROM global_macro_vintages
                WHERE source='alfred_vintages'
                GROUP BY series_id, series_name
                ORDER BY series_id
                """
            )
        ]

        if int(row["rows"] or 0) == 0:
            failures.append("no ALFRED rows")
        if int(row["series"] or 0) != len(SERIES):
            failures.append(f"expected {len(SERIES)} series, found {row['series']}")
        if missing:
            failures.append(f"null contract rows: {missing}")
        if reversed_time:
            failures.append(f"availability before observation: {reversed_time}")
        if non_conservative:
            failures.append(f"same-day availability rows: {non_conservative}")
        if duplicate_groups:
            failures.append(f"duplicate vintage groups: {duplicate_groups}")
        if authorities is None:
            failures.append("missing source registry")
        elif authorities["trading_authority"] or authorities["historical_research_authority"]:
            failures.append("ALFRED authority was enabled before validation")
        if health is None or health["status"] != "ACTIVE":
            failures.append(f"source health is {health['status'] if health else 'MISSING'}")

        report = {
            "status": "PASSED" if not failures else "FAILED",
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "alfred_vintages",
            "mode": "SHADOW_CONTEXT_ONLY",
            "authority": {
                "trading": False,
                "historical_backtest": False,
                "state": authorities["state"] if authorities else "MISSING",
            },
            "contract": {
                "vintage_kind": "INITIAL_RELEASE",
                "availability": "NEXT_UTC_DAY_AFTER_REALTIME_START",
                "rows": row["rows"],
                "series": row["series"],
                "missing_rows": missing,
                "reversed_time_rows": reversed_time,
                "non_conservative_rows": non_conservative,
                "duplicate_groups": duplicate_groups,
            },
            "health": dict(health) if health else None,
            "series_detail": series_rows,
            "failures": failures,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if failures:
            raise SystemExit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
