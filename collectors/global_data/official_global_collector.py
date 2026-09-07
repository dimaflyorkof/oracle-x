from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from database.db import connect


BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FED_RSS_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
BLS_CALENDAR_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
CFTC_COT_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
CFTC_BTC_CONTRACT = "133741"
USER_AGENT = "ORACLE-X-Global-Data-Layer/1.0"

BLS_SERIES = {
    "CUUR0000SA0": ("US_CPI_ALL", "index"),
    "CUUR0000SA0L1E": ("US_CPI_CORE", "index"),
    "WPSFD4": ("US_PPI_FINAL_DEMAND", "index"),
    "CES0000000001": ("US_NONFARM_PAYROLLS", "thousands"),
    "LNS14000000": ("US_UNEMPLOYMENT_RATE", "percent"),
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def unix(dt: datetime) -> int:
    return int(dt.timestamp())


def ensure_schema(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS global_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            dataset TEXT NOT NULL,
            series_id TEXT NOT NULL,
            series_name TEXT NOT NULL,
            reference_period TEXT NOT NULL,
            reference_timestamp TEXT,
            reference_timestamp_unix INTEGER,
            available_at TEXT NOT NULL,
            available_at_unix INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            observed_at_unix INTEGER NOT NULL,
            value REAL,
            unit TEXT,
            frequency TEXT,
            revision INTEGER NOT NULL DEFAULT 1,
            data_kind TEXT NOT NULL,
            quality TEXT NOT NULL DEFAULT 'OBSERVED',
            raw_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source, series_id, reference_period, revision)
        );

        CREATE INDEX IF NOT EXISTS idx_global_observations_available
        ON global_observations(available_at_unix, source, series_id);

        CREATE INDEX IF NOT EXISTS idx_global_observations_reference
        ON global_observations(series_id, reference_timestamp_unix);

        CREATE TABLE IF NOT EXISTS global_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            external_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            title TEXT NOT NULL,
            event_timestamp TEXT,
            event_timestamp_unix INTEGER,
            available_at TEXT NOT NULL,
            available_at_unix INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            observed_at_unix INTEGER NOT NULL,
            url TEXT,
            data_kind TEXT NOT NULL,
            quality TEXT NOT NULL DEFAULT 'OFFICIAL',
            raw_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source, external_id)
        );

        CREATE INDEX IF NOT EXISTS idx_global_events_available
        ON global_events(available_at_unix, event_type);

        CREATE TABLE IF NOT EXISTS global_source_health (
            source TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            last_attempt_at TEXT NOT NULL,
            last_attempt_unix INTEGER NOT NULL,
            last_success_at TEXT,
            last_success_unix INTEGER,
            records_seen INTEGER NOT NULL DEFAULT 0,
            records_changed INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS global_source_registry (
            source TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            authority TEXT NOT NULL,
            endpoint TEXT,
            trust_tier TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 0,
            trading_authority INTEGER NOT NULL DEFAULT 0,
            historical_research_authority INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS global_positioning (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            market_name TEXT NOT NULL,
            contract_code TEXT NOT NULL,
            report_date TEXT NOT NULL,
            report_timestamp_unix INTEGER NOT NULL,
            available_at TEXT NOT NULL,
            available_at_unix INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            observed_at_unix INTEGER NOT NULL,
            open_interest REAL,
            noncommercial_long REAL,
            noncommercial_short REAL,
            noncommercial_spread REAL,
            commercial_long REAL,
            commercial_short REAL,
            nonreportable_long REAL,
            nonreportable_short REAL,
            noncommercial_net REAL,
            commercial_net REAL,
            revision INTEGER NOT NULL DEFAULT 1,
            fingerprint TEXT NOT NULL,
            data_kind TEXT NOT NULL,
            quality TEXT NOT NULL,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source, contract_code, report_date, revision)
        );

        CREATE INDEX IF NOT EXISTS idx_global_positioning_available
        ON global_positioning(available_at_unix, contract_code);
        """
    )
    con.commit()


def request_bytes(url: str, payload: Optional[bytes] = None) -> bytes:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, text/xml"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return response.read()


def update_health(con, source: str, status: str, seen: int, changed: int, error: Optional[str]) -> None:
    now = utc_now()
    success_at = iso(now) if status == "ACTIVE" else None
    success_unix = unix(now) if status == "ACTIVE" else None
    con.execute(
        """
        INSERT INTO global_source_health (
            source, status, last_attempt_at, last_attempt_unix,
            last_success_at, last_success_unix, records_seen,
            records_changed, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            status=excluded.status,
            last_attempt_at=excluded.last_attempt_at,
            last_attempt_unix=excluded.last_attempt_unix,
            last_success_at=COALESCE(excluded.last_success_at, global_source_health.last_success_at),
            last_success_unix=COALESCE(excluded.last_success_unix, global_source_health.last_success_unix),
            records_seen=excluded.records_seen,
            records_changed=excluded.records_changed,
            error=excluded.error
        """,
        (source, status, iso(now), unix(now), success_at, success_unix, seen, changed, error),
    )
    con.commit()


def month_timestamp(year: str, period: str) -> datetime:
    return datetime(int(year), int(period[1:]), 1, tzinfo=timezone.utc)


def numeric_value(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text in {"", "-", ".", "N/A", "NA"}:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def store_observation(con, item: Dict[str, Any], observed: datetime) -> bool:
    previous = con.execute(
        """
        SELECT value, revision FROM global_observations
        WHERE source=? AND series_id=? AND reference_period=?
        ORDER BY revision DESC LIMIT 1
        """,
        (item["source"], item["series_id"], item["reference_period"]),
    ).fetchone()
    value = numeric_value(item["value"])
    if value is None:
        return False
    if previous is not None and previous["value"] == value:
        return False
    revision = int(previous["revision"]) + 1 if previous is not None else 1
    ref = item["reference_datetime"]
    con.execute(
        """
        INSERT INTO global_observations (
            source, dataset, series_id, series_name, reference_period,
            reference_timestamp, reference_timestamp_unix,
            available_at, available_at_unix, observed_at, observed_at_unix,
            value, unit, frequency, revision, data_kind, quality,
            raw_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item["source"], item["dataset"], item["series_id"], item["series_name"],
            item["reference_period"], iso(ref), unix(ref), iso(observed), unix(observed),
            iso(observed), unix(observed), value, item["unit"], item["frequency"],
            revision, "LIVE_OBSERVED", "OFFICIAL_VALUE_CAUSAL_FROM_OBSERVED_AT",
            json.dumps(item["raw"], ensure_ascii=False, separators=(",", ":")), iso(observed),
        ),
    )
    return True


def collect_bls(con) -> Dict[str, Any]:
    source = "bls_public_data_api"
    observed = utc_now()
    years = [str(observed.year - 1), str(observed.year)]
    body: Dict[str, Any] = {"seriesid": list(BLS_SERIES), "startyear": years[0], "endyear": years[-1]}
    key = os.getenv("BLS_API_KEY", "").strip()
    if key:
        body["registrationkey"] = key
    try:
        raw = request_bytes(BLS_URL, json.dumps(body).encode("utf-8"))
        payload = json.loads(raw.decode("utf-8"))
        if payload.get("status") != "REQUEST_SUCCEEDED":
            raise RuntimeError("; ".join(payload.get("message") or ["BLS request failed"]))
        seen = changed = skipped_missing = 0
        for series in payload.get("Results", {}).get("series", []):
            series_id = series.get("seriesID")
            if series_id not in BLS_SERIES:
                continue
            series_name, unit = BLS_SERIES[series_id]
            for point in series.get("data", []):
                period = str(point.get("period", ""))
                if not period.startswith("M") or period == "M13":
                    continue
                seen += 1
                if numeric_value(point.get("value")) is None:
                    skipped_missing += 1
                    continue
                year = str(point["year"])
                item = {
                    "source": source,
                    "dataset": "US_LABOR_AND_PRICES",
                    "series_id": series_id,
                    "series_name": series_name,
                    "reference_period": f"{year}-{period[1:]}",
                    "reference_datetime": month_timestamp(year, period),
                    "value": point["value"],
                    "unit": unit,
                    "frequency": "MONTHLY",
                    "raw": point,
                }
                changed += int(store_observation(con, item, observed))
        con.commit()
        update_health(con, source, "ACTIVE", seen, changed, None)
        return {
            "source": source,
            "status": "ACTIVE",
            "seen": seen,
            "changed": changed,
            "skipped_missing": skipped_missing,
            "key": bool(key),
        }
    except Exception as exc:
        update_health(con, source, "DEGRADED", 0, 0, str(exc)[:500])
        return {"source": source, "status": "DEGRADED", "error": str(exc)}


def text_of(node: ET.Element, name: str) -> str:
    child = node.find(name)
    return (child.text or "").strip() if child is not None else ""


def parse_fed_items(raw: bytes) -> Iterable[Dict[str, Any]]:
    root = ET.fromstring(raw)
    for item in root.findall("./channel/item"):
        title = text_of(item, "title")
        link = text_of(item, "link")
        guid = text_of(item, "guid") or link or title
        published_text = text_of(item, "pubDate")
        if not published_text:
            continue
        published = parsedate_to_datetime(published_text)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        published = published.astimezone(timezone.utc)
        yield {
            "external_id": hashlib.sha256(guid.encode("utf-8")).hexdigest(),
            "title": title,
            "url": link,
            "published": published,
            "raw": {"guid": guid, "pubDate": published_text},
        }


def collect_fed(con) -> Dict[str, Any]:
    source = "federal_reserve_monetary_rss"
    observed = utc_now()
    try:
        items = list(parse_fed_items(request_bytes(FED_RSS_URL)))
        changed = 0
        for item in items:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO global_events (
                    source, external_id, event_type, title,
                    event_timestamp, event_timestamp_unix,
                    available_at, available_at_unix, observed_at, observed_at_unix,
                    url, data_kind, quality, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source, item["external_id"], "CENTRAL_BANK_PUBLICATION", item["title"],
                    iso(item["published"]), unix(item["published"]),
                    iso(item["published"]), unix(item["published"]),
                    iso(observed), unix(observed), item["url"], "OFFICIAL_PUBLICATION",
                    "OFFICIAL_RSS_TIMESTAMP", json.dumps(item["raw"], ensure_ascii=False), iso(observed),
                ),
            )
            changed += max(cur.rowcount, 0)
        con.commit()
        update_health(con, source, "ACTIVE", len(items), changed, None)
        return {"source": source, "status": "ACTIVE", "seen": len(items), "changed": changed}
    except Exception as exc:
        update_health(con, source, "DEGRADED", 0, 0, str(exc)[:500])
        return {"source": source, "status": "DEGRADED", "error": str(exc)}


def unfold_ics(raw: bytes) -> list[str]:
    text = raw.decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
    unfolded: list[str] = []
    for line in text.split("\n"):
        if line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    return unfolded


def parse_ics_datetime(field: str, value: str) -> datetime:
    timezone_name = "America/New_York"
    for token in field.split(";")[1:]:
        if token.startswith("TZID="):
            timezone_name = token.split("=", 1)[1]
    timezone_aliases = {
        "US-Eastern": "America/New_York",
        "US/Eastern": "America/New_York",
        "Eastern Standard Time": "America/New_York",
    }
    timezone_name = timezone_aliases.get(timezone_name, timezone_name)
    value = value.strip()
    if value.endswith("Z"):
        pattern = "%Y%m%dT%H%M%SZ" if len(value) == 16 else "%Y%m%dT%H%MZ"
        return datetime.strptime(value, pattern).replace(tzinfo=timezone.utc)
    if "T" in value:
        pattern = "%Y%m%dT%H%M%S" if len(value) == 15 else "%Y%m%dT%H%M"
        parsed = datetime.strptime(value, pattern)
    else:
        parsed = datetime.strptime(value, "%Y%m%d")
    return parsed.replace(tzinfo=ZoneInfo(timezone_name)).astimezone(timezone.utc)


def parse_bls_calendar(raw: bytes) -> Iterable[Dict[str, Any]]:
    current: Optional[Dict[str, str]] = None
    for line in unfold_ics(raw):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT" and current is not None:
            dt_field = next((key for key in current if key.startswith("DTSTART")), None)
            if dt_field and current.get("SUMMARY"):
                scheduled = parse_ics_datetime(dt_field, current[dt_field])
                uid = current.get("UID", current["SUMMARY"])
                identity = f"{uid}|{scheduled.isoformat()}|{current['SUMMARY']}"
                yield {
                    "external_id": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
                    "title": current["SUMMARY"].replace("\\,", ","),
                    "scheduled": scheduled,
                    "raw": current,
                }
            current = None
            continue
        if current is not None and ":" in line:
            key, value = line.split(":", 1)
            current[key] = value


def collect_bls_calendar(con) -> Dict[str, Any]:
    source = "official_release_calendar"
    observed = utc_now()
    try:
        items = list(parse_bls_calendar(request_bytes(BLS_CALENDAR_URL)))
        changed = 0
        for item in items:
            cur = con.execute(
                """
                INSERT OR IGNORE INTO global_events (
                    source, external_id, event_type, title,
                    event_timestamp, event_timestamp_unix,
                    available_at, available_at_unix, observed_at, observed_at_unix,
                    url, data_kind, quality, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source, item["external_id"], "ECONOMIC_RELEASE_SCHEDULE", item["title"],
                    iso(item["scheduled"]), unix(item["scheduled"]),
                    iso(observed), unix(observed), iso(observed), unix(observed),
                    BLS_CALENDAR_URL, "OFFICIAL_SCHEDULE_SNAPSHOT",
                    "OFFICIAL_CALENDAR_OBSERVED_AT", json.dumps(item["raw"], ensure_ascii=False), iso(observed),
                ),
            )
            changed += max(cur.rowcount, 0)
        con.commit()
        update_health(con, source, "ACTIVE", len(items), changed, None)
        return {"source": source, "status": "ACTIVE", "seen": len(items), "changed": changed}
    except Exception as exc:
        update_health(con, source, "DEGRADED", 0, 0, str(exc)[:500])
        return {"source": source, "status": "DEGRADED", "error": str(exc)}


def first_value(row: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return None


def parse_cftc_date(value: str) -> datetime:
    clean = str(value).split("T", 1)[0]
    pattern = "%Y-%m-%d" if "-" in clean else "%Y%m%d"
    return datetime.strptime(clean, pattern).replace(tzinfo=timezone.utc)


def cftc_number(row: Dict[str, Any], *keys: str) -> Optional[float]:
    return numeric_value(first_value(row, *keys))


def store_cftc_position(con, row: Dict[str, Any], observed: datetime) -> bool:
    report_value = first_value(row, "report_date_as_yyyy_mm_dd", "as_of_date_in_form_yyymmdd")
    if not report_value:
        return False
    report_dt = parse_cftc_date(str(report_value))
    report_date = report_dt.date().isoformat()
    market_name = str(first_value(row, "market_and_exchange_names", "market_and_exchange_name") or "BITCOIN - CME")
    contract_code = str(first_value(row, "cftc_contract_market_code") or CFTC_BTC_CONTRACT)
    values = {
        "open_interest": cftc_number(row, "open_interest_all"),
        "noncommercial_long": cftc_number(row, "noncomm_positions_long_all"),
        "noncommercial_short": cftc_number(row, "noncomm_positions_short_all"),
        "noncommercial_spread": cftc_number(row, "noncomm_postions_spread_all", "noncomm_positions_spread_all"),
        "commercial_long": cftc_number(row, "comm_positions_long_all"),
        "commercial_short": cftc_number(row, "comm_positions_short_all"),
        "nonreportable_long": cftc_number(row, "nonrept_positions_long_all"),
        "nonreportable_short": cftc_number(row, "nonrept_positions_short_all"),
    }
    fingerprint = hashlib.sha256(json.dumps(values, sort_keys=True).encode("utf-8")).hexdigest()
    previous = con.execute(
        """SELECT revision, fingerprint FROM global_positioning
           WHERE source='cftc_cot' AND contract_code=? AND report_date=?
           ORDER BY revision DESC LIMIT 1""",
        (contract_code, report_date),
    ).fetchone()
    if previous is not None and previous["fingerprint"] == fingerprint:
        return False
    revision = int(previous["revision"]) + 1 if previous is not None else 1
    noncomm_net = None
    if values["noncommercial_long"] is not None and values["noncommercial_short"] is not None:
        noncomm_net = values["noncommercial_long"] - values["noncommercial_short"]
    comm_net = None
    if values["commercial_long"] is not None and values["commercial_short"] is not None:
        comm_net = values["commercial_long"] - values["commercial_short"]
    con.execute(
        """
        INSERT INTO global_positioning (
            source, market_name, contract_code, report_date, report_timestamp_unix,
            available_at, available_at_unix, observed_at, observed_at_unix,
            open_interest, noncommercial_long, noncommercial_short, noncommercial_spread,
            commercial_long, commercial_short, nonreportable_long, nonreportable_short,
            noncommercial_net, commercial_net, revision, fingerprint,
            data_kind, quality, raw_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "cftc_cot", market_name, contract_code, report_date, unix(report_dt),
            iso(observed), unix(observed), iso(observed), unix(observed),
            values["open_interest"], values["noncommercial_long"], values["noncommercial_short"],
            values["noncommercial_spread"], values["commercial_long"], values["commercial_short"],
            values["nonreportable_long"], values["nonreportable_short"], noncomm_net, comm_net,
            revision, fingerprint, "OFFICIAL_POSITIONING_OBSERVED_LIVE",
            "CAUSAL_FROM_FIRST_OBSERVED_AT", json.dumps(row, ensure_ascii=False), iso(observed),
        ),
    )
    return True


def collect_cftc(con) -> Dict[str, Any]:
    source = "cftc_cot"
    observed = utc_now()
    query = urllib.parse.urlencode({
        "$where": f"cftc_contract_market_code='{CFTC_BTC_CONTRACT}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": "260",
    })
    try:
        rows = json.loads(request_bytes(f"{CFTC_COT_URL}?{query}").decode("utf-8"))
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected CFTC response")
        changed = sum(int(store_cftc_position(con, row, observed)) for row in rows)
        con.commit()
        update_health(con, source, "ACTIVE", len(rows), changed, None)
        return {"source": source, "status": "ACTIVE", "seen": len(rows), "changed": changed}
    except Exception as exc:
        update_health(con, source, "DEGRADED", 0, 0, str(exc)[:500])
        return {"source": source, "status": "DEGRADED", "error": str(exc)}


def register_sources(con) -> None:
    now = utc_now()
    registry = (
        ("bls_public_data_api", "MACRO", "U.S. Bureau of Labor Statistics", BLS_URL, "OFFICIAL_PRIMARY", 1, "CONTEXT_ONLY", "Live observations; historical release-time backfill not yet authorized"),
        ("federal_reserve_monetary_rss", "CENTRAL_BANK", "Board of Governors of the Federal Reserve System", FED_RSS_URL, "OFFICIAL_PRIMARY", 1, "CONTEXT_ONLY", "Official publication timestamps"),
        ("alfred_vintages", "MACRO", "Federal Reserve Bank of St. Louis", "https://api.stlouisfed.org/fred/", "OFFICIAL_PRIMARY", 0, "PENDING", "Requires API key and vintage-time contract"),
        ("cftc_cot", "POSITIONING", "U.S. Commodity Futures Trading Commission", CFTC_COT_URL, "OFFICIAL_PRIMARY", 1, "CONTEXT_ONLY", "CME Bitcoin contract 133741; historical rows causal only from first observed_at"),
        ("cme_positioning", "INSTITUTIONAL", "CME Group", None, "OFFICIAL_LICENSED", 0, "PENDING", "Use only through an authorized market-data agreement"),
        ("btc_etf_flows", "INSTITUTIONAL", "SEC and fund issuers", None, "OFFICIAL_FRAGMENTED", 0, "PENDING", "No single official free consolidated daily-flow API"),
        ("official_release_calendar", "EVENT_CALENDAR", "U.S. Bureau of Labor Statistics", BLS_CALENDAR_URL, "OFFICIAL_PRIMARY", 1, "CONTEXT_ONLY", "Official BLS calendar snapshots with Eastern-time conversion"),
    )
    for source, category, authority, endpoint, trust, enabled, state, notes in registry:
        con.execute(
            """
            INSERT INTO global_source_registry (
                source, category, authority, endpoint, trust_tier, enabled,
                trading_authority, historical_research_authority, state, notes
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                category=excluded.category,
                authority=excluded.authority,
                endpoint=excluded.endpoint,
                trust_tier=excluded.trust_tier,
                enabled=excluded.enabled,
                trading_authority=0,
                historical_research_authority=0,
                state=excluded.state,
                notes=excluded.notes
            """,
            (source, category, authority, endpoint, trust, enabled, state, notes),
        )
    for source in ("alfred_vintages", "cme_positioning", "btc_etf_flows"):
        con.execute(
            """
            INSERT OR IGNORE INTO global_source_health (
                source, status, last_attempt_at, last_attempt_unix,
                records_seen, records_changed, error
            ) VALUES (?, 'PENDING', ?, ?, 0, 0, 'Not enabled in Global Data Layer V1')
            """,
            (source, iso(now), unix(now)),
        )
    con.commit()


def run_once() -> Dict[str, Any]:
    con = connect()
    try:
        ensure_schema(con)
        register_sources(con)
        results = [collect_bls(con), collect_fed(con), collect_bls_calendar(con), collect_cftc(con)]
        return {"status": "COMPLETE", "policy": "CONTEXT_ONLY_NO_TRADING_AUTHORITY", "results": results}
    finally:
        con.close()


def main() -> None:
    print(json.dumps(run_once(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
