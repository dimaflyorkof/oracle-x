from __future__ import annotations

import json

from database.db import connect, init_database


REQUIRED_COLUMNS = {
    "event_timestamp_unix",
    "available_at_unix",
    "data_interval_seconds",
    "data_kind",
}


def scalar(con, sql: str, params=()) -> int:
    return int(con.execute(sql, params).fetchone()[0])


def main() -> None:
    init_database()
    con = connect()
    try:
        columns = {
            str(row["name"])
            for row in con.execute("PRAGMA table_info(derivatives_history)")
        }
        missing_columns = sorted(REQUIRED_COLUMNS - columns)
        null_contract_rows = scalar(
            con,
            """
            SELECT COUNT(*)
            FROM derivatives_history
            WHERE event_timestamp_unix IS NULL
               OR available_at_unix IS NULL
               OR data_interval_seconds IS NULL
               OR data_kind IS NULL
            """,
        )
        reversed_time_rows = scalar(
            con,
            """
            SELECT COUNT(*)
            FROM derivatives_history
            WHERE available_at_unix < event_timestamp_unix
            """,
        )
        early_hourly_rows = scalar(
            con,
            """
            SELECT COUNT(*)
            FROM derivatives_history
            WHERE data_kind = 'HISTORICAL_HOURLY_AGGREGATE'
              AND available_at_unix < event_timestamp_unix + 3600
            """,
        )
        early_live_rows = scalar(
            con,
            """
            SELECT COUNT(*)
            FROM derivatives_history
            WHERE data_kind = 'LIVE_5M_COMPOSITE'
              AND available_at_unix < event_timestamp_unix + 300
            """,
        )
        misclassified_funding_events = scalar(
            con,
            """
            SELECT COUNT(*)
            FROM derivatives_history
            WHERE funding_rate IS NOT NULL
              AND open_interest IS NULL
              AND long_short_ratio IS NULL
              AND taker_ratio IS NULL
              AND (
                  data_kind != 'EVENT'
                  OR data_interval_seconds != 0
                  OR available_at_unix != event_timestamp_unix
              )
            """,
        )
        kinds = [
            dict(row)
            for row in con.execute(
                """
                SELECT data_kind, source, COUNT(*) AS rows,
                       MIN(event_timestamp_unix) AS first_event,
                       MAX(event_timestamp_unix) AS last_event,
                       MIN(available_at_unix) AS first_available,
                       MAX(available_at_unix) AS last_available
                FROM derivatives_history
                GROUP BY data_kind, source
                ORDER BY source, data_kind
                """
            ).fetchall()
        ]
        sample = con.execute(
            """
            SELECT id, event_timestamp_unix, available_at_unix
            FROM derivatives_history
            WHERE data_kind = 'HISTORICAL_HOURLY_AGGREGATE'
              AND taker_ratio IS NOT NULL
            ORDER BY event_timestamp_unix ASC
            LIMIT 1
            """
        ).fetchone()
        sample_visible_after_15m = None
        if sample is not None:
            sample_visible_after_15m = scalar(
                con,
                """
                SELECT COUNT(*)
                FROM derivatives_history
                WHERE id = ?
                  AND available_at_unix <= ?
                """,
                (
                    int(sample["id"]),
                    int(sample["event_timestamp_unix"]) + 900,
                ),
            )
        failures = []
        if missing_columns:
            failures.append("missing contract columns")
        if null_contract_rows:
            failures.append("rows without complete contract")
        if reversed_time_rows:
            failures.append("available_at precedes event time")
        if early_hourly_rows:
            failures.append("hourly aggregate available before hour close")
        if early_live_rows:
            failures.append("live 5m row available before interval close")
        if misclassified_funding_events:
            failures.append("funding-only events have incorrect data kind")
        if sample_visible_after_15m not in (None, 0):
            failures.append("historical hourly lookahead remains visible")
        report = {
            "status": "PASSED" if not failures else "FAILED",
            "missing_columns": missing_columns,
            "null_contract_rows": null_contract_rows,
            "reversed_time_rows": reversed_time_rows,
            "early_hourly_rows": early_hourly_rows,
            "early_live_rows": early_live_rows,
            "misclassified_funding_events": misclassified_funding_events,
            "historical_sample_visible_after_15m": sample_visible_after_15m,
            "datasets": kinds,
            "failures": failures,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if failures:
            raise SystemExit(1)
    finally:
        con.close()


if __name__ == "__main__":
    main()
