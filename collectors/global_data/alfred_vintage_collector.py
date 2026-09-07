from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from urllib.error import HTTPError
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, Optional

from collectors.global_data.official_global_collector import ensure_schema, iso, unix, update_health
from database.db import connect


SOURCE = "alfred_vintages"
ENDPOINT = "https://api.stlouisfed.org/fred/series/observations"
USER_AGENT = "ORACLE-X-ALFRED-Vintage-Collector/1.2.2"
OBSERVATION_START = os.getenv("ALFRED_OBSERVATION_START", "2023-09-01")
API_KEY_PATTERN = re.compile(r"^[a-z0-9]{32}$")

# Stable, liquid-market-relevant U.S. macro series.  Only first-release
# vintages are requested; revised values are deliberately excluded.
SERIES: Dict[str, tuple[str, str]] = {
    "CPIAUCSL": ("US_CPI_ALL", "PRICES"),
    "CPILFESL": ("US_CPI_CORE", "PRICES"),
    "PAYEMS": ("US_NONFARM_PAYROLLS", "LABOR"),
    "UNRATE": ("US_UNEMPLOYMENT_RATE", "LABOR"),
    "DFF": ("FED_FUNDS_EFFECTIVE_RATE", "RATES"),
    "DGS10": ("US_TREASURY_10Y", "RATES"),
    "DTWEXBGS": ("US_DOLLAR_BROAD_INDEX", "FX"),
    "VIXCLS": ("VIX_CLOSE", "RISK"),
    "BAMLH0A0HYM2": ("US_HIGH_YIELD_OAS", "CREDIT"),
    "NFCI": ("CHICAGO_FED_NFCI", "FINANCIAL_CONDITIONS"),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_number(value: Any) -> Optional[float]:
    text = str(value).strip() if value is not None else ""
    if text in {"", ".", "-", "NA", "N/A"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def safe_error(exc: Exception, api_key: str = "") -> str:
    message = str(exc)
    if api_key:
        message = message.replace(api_key, "[REDACTED]")
    return message[:500]


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def at_utc_midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def conservative_available_at(realtime_start: date) -> datetime:
    # ALFRED exposes the day on which a vintage became real-time information,
    # but not a guaranteed intraday timestamp for every series.  Advancing to
    # 00:00 UTC on the following day prevents same-day lookahead.
    return at_utc_midnight(realtime_start + timedelta(days=1))


def ensure_alfred_schema(con) -> None:
    ensure_schema(con)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS global_macro_vintages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            series_id TEXT NOT NULL,
            series_name TEXT NOT NULL,
            category TEXT NOT NULL,
            observation_date TEXT NOT NULL,
            observation_timestamp_unix INTEGER NOT NULL,
            realtime_start TEXT NOT NULL,
            realtime_end TEXT,
            available_at TEXT NOT NULL,
            available_at_unix INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            observed_at_unix INTEGER NOT NULL,
            value REAL NOT NULL,
            unit TEXT,
            frequency TEXT,
            vintage_kind TEXT NOT NULL,
            data_kind TEXT NOT NULL,
            quality TEXT NOT NULL,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source, series_id, observation_date, realtime_start)
        );

        CREATE INDEX IF NOT EXISTS idx_global_macro_vintages_available
        ON global_macro_vintages(available_at_unix, series_id);

        CREATE INDEX IF NOT EXISTS idx_global_macro_vintages_observation
        ON global_macro_vintages(series_id, observation_timestamp_unix);
        """
    )
    con.commit()


def register_source(con, state: str, note: str) -> None:
    con.execute(
        """
        INSERT INTO global_source_registry (
            source, category, authority, endpoint, trust_tier, enabled,
            trading_authority, historical_research_authority, state, notes
        ) VALUES (?, 'MACRO', 'Federal Reserve Bank of St. Louis', ?,
                  'OFFICIAL_PRIMARY', 1, 0, 0, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            category=excluded.category,
            authority=excluded.authority,
            endpoint=excluded.endpoint,
            trust_tier=excluded.trust_tier,
            enabled=1,
            trading_authority=0,
            historical_research_authority=0,
            state=excluded.state,
            notes=excluded.notes
        """,
        (SOURCE, ENDPOINT, state, note),
    )
    con.commit()


def fetch_series(api_key: str, series_id: str) -> Dict[str, Any]:
    params = urllib.parse.urlencode(
        {
            "api_key": api_key,
            "file_type": "json",
            "series_id": series_id,
            "observation_start": OBSERVATION_START,
            # A full real-time range can exceed FRED's vintage-date limit for
            # daily series even when observation_start is recent.  We need no
            # vintage before the research window begins.
            "realtime_start": OBSERVATION_START,
            "realtime_end": "9999-12-31",
            "output_type": "4",
            "sort_order": "asc",
            "limit": "100000",
        }
    )
    request = urllib.request.Request(
        f"{ENDPOINT}?{params}",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode("utf-8", errors="replace"))
            detail = str(error_payload.get("error_message") or "Bad request")
        except Exception:
            detail = "Bad request"
        raise RuntimeError(f"{series_id}: FRED HTTP {exc.code}: {detail}") from None
    if "error_code" in payload or not isinstance(payload.get("observations"), list):
        raise RuntimeError(str(payload.get("error_message") or "Unexpected ALFRED response"))
    return payload


def store_series(
    con,
    series_id: str,
    payload: Dict[str, Any],
    observed: datetime,
) -> tuple[int, int, int, int]:
    series_name, category = SERIES[series_id]
    unit = payload.get("units")
    frequency = payload.get("frequency_short") or payload.get("frequency")
    seen = changed = skipped = rejected_temporal = 0
    for row in payload.get("observations", []):
        seen += 1
        value = parse_number(row.get("value"))
        if value is None:
            skipped += 1
            continue
        observation_date = parse_date(str(row["date"]))
        realtime_start = parse_date(str(row["realtime_start"]))
        available = conservative_available_at(realtime_start)
        observation_timestamp = unix(at_utc_midnight(observation_date))
        if unix(available) < observation_timestamp:
            # Some daily ALFRED initial-release rows can carry a prior
            # business-day value under a later U.S. holiday date.  Such a row
            # violates the event-time contract and is excluded, never shifted.
            rejected_temporal += 1
            continue
        cur = con.execute(
            """
            INSERT OR IGNORE INTO global_macro_vintages (
                source, series_id, series_name, category,
                observation_date, observation_timestamp_unix,
                realtime_start, realtime_end,
                available_at, available_at_unix,
                observed_at, observed_at_unix,
                value, unit, frequency, vintage_kind, data_kind, quality,
                raw_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                SOURCE,
                series_id,
                series_name,
                category,
                observation_date.isoformat(),
                observation_timestamp,
                realtime_start.isoformat(),
                row.get("realtime_end"),
                iso(available),
                unix(available),
                iso(observed),
                unix(observed),
                value,
                unit,
                frequency,
                "INITIAL_RELEASE",
                "ALFRED_INITIAL_RELEASE",
                "DATE_ONLY_CONSERVATIVE_NEXT_DAY",
                json.dumps(row, ensure_ascii=False, separators=(",", ":")),
                iso(observed),
            ),
        )
        changed += max(cur.rowcount, 0)
    return seen, changed, skipped, rejected_temporal


def run_once() -> Dict[str, Any]:
    api_key = os.getenv("FRED_API_KEY", "").strip()
    con = connect()
    try:
        ensure_alfred_schema(con)
        if not API_KEY_PATTERN.fullmatch(api_key):
            note = "FRED_API_KEY missing or invalid; secret was not logged"
            register_source(con, "PENDING_API_KEY", note)
            update_health(con, SOURCE, "PENDING", 0, 0, note)
            return {"status": "PENDING_API_KEY", "source": SOURCE, "key_exposed": False}

        total_seen = total_changed = total_skipped = total_rejected_temporal = 0
        reports = []
        try:
            payloads = {
                series_id: fetch_series(api_key, series_id)
                for series_id in SERIES
            }
            cleaned_temporal = max(
                con.execute(
                    """
                    DELETE FROM global_macro_vintages
                    WHERE source=?
                      AND available_at_unix < observation_timestamp_unix
                    """,
                    (SOURCE,),
                ).rowcount,
                0,
            )
            for series_id, payload in payloads.items():
                seen, changed, skipped, rejected_temporal = store_series(
                    con, series_id, payload, utc_now()
                )
                total_seen += seen
                total_changed += changed
                total_skipped += skipped
                total_rejected_temporal += rejected_temporal
                reports.append(
                    {
                        "series_id": series_id,
                        "seen": seen,
                        "inserted": changed,
                        "skipped_missing": skipped,
                        "rejected_temporal": rejected_temporal,
                    }
                )
            con.commit()
            register_source(
                con,
                "BACKTEST_CANDIDATE",
                "Initial-release vintages; conservative next-day availability; no trading authority",
            )
            update_health(
                con,
                SOURCE,
                "ACTIVE",
                total_seen,
                total_changed + cleaned_temporal,
                None,
            )
            return {
                "status": "COMPLETE",
                "source": SOURCE,
                "mode": "SHADOW_CONTEXT_ONLY",
                "trading_authority": False,
                "historical_research_authority": False,
                "observation_start": OBSERVATION_START,
                "series": reports,
                "seen": total_seen,
                "inserted": total_changed,
                "skipped_missing": total_skipped,
                "rejected_temporal": total_rejected_temporal,
                "cleaned_previous_temporal_rows": cleaned_temporal,
                "key_exposed": False,
            }
        except Exception as exc:
            con.rollback()
            error = safe_error(exc, api_key)
            register_source(con, "DEGRADED", "ALFRED collection failed; no authority granted")
            update_health(con, SOURCE, "DEGRADED", total_seen, total_changed, error)
            return {
                "status": "DEGRADED",
                "source": SOURCE,
                "error": error,
                "key_exposed": False,
            }
    finally:
        con.close()


def main() -> None:
    result = run_once()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["status"] != "COMPLETE":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
