from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, Optional

from database.db import connect


BLS_URL = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
FED_RSS_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
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


def register_sources(con) -> None:
    now = utc_now()
    registry = (
        ("bls_public_data_api", "MACRO", "U.S. Bureau of Labor Statistics", BLS_URL, "OFFICIAL_PRIMARY", 1, "CONTEXT_ONLY", "Live observations; historical release-time backfill not yet authorized"),
        ("federal_reserve_monetary_rss", "CENTRAL_BANK", "Board of Governors of the Federal Reserve System", FED_RSS_URL, "OFFICIAL_PRIMARY", 1, "CONTEXT_ONLY", "Official publication timestamps"),
        ("alfred_vintages", "MACRO", "Federal Reserve Bank of St. Louis", "https://api.stlouisfed.org/fred/", "OFFICIAL_PRIMARY", 0, "PENDING", "Requires API key and vintage-time contract"),
        ("cftc_cot", "POSITIONING", "U.S. Commodity Futures Trading Commission", "https://www.cftc.gov/MarketReports/CommitmentsofTraders/index.htm", "OFFICIAL_PRIMARY", 0, "PENDING", "Requires BTC contract mapping and publication-time validation"),
        ("cme_positioning", "INSTITUTIONAL", "CME Group", None, "OFFICIAL_LICENSED", 0, "PENDING", "Use only through an authorized market-data agreement"),
        ("btc_etf_flows", "INSTITUTIONAL", "SEC and fund issuers", None, "OFFICIAL_FRAGMENTED", 0, "PENDING", "No single official free consolidated daily-flow API"),
        ("official_release_calendar", "EVENT_CALENDAR", "BLS / Federal Reserve / BEA", None, "OFFICIAL_PRIMARY", 0, "PENDING", "Exact release timestamps must be validated per agency"),
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
    for source in ("alfred_vintages", "cftc_cot", "cme_positioning", "btc_etf_flows", "official_release_calendar"):
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
        results = [collect_bls(con), collect_fed(con)]
        return {"status": "COMPLETE", "policy": "CONTEXT_ONLY_NO_TRADING_AUTHORITY", "results": results}
    finally:
        con.close()


def main() -> None:
    print(json.dumps(run_once(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
