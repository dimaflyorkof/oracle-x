from __future__ import annotations

import csv
import hashlib
import http.cookiejar
import io
import json
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, Optional

IBIT_PAGE_URL = "https://www.ishares.com/us/products/333011/ishares-bitcoin-trust"
IBIT_URL = (
    "https://www.ishares.com/us/products/333011/fund/1467271812596.ajax"
    "?dataType=fund&fileName=IBIT_holdings&fileType=csv"
)
IBIT_FALLBACK_URL = (
    "https://www.ishares.com/us/products/333011/"
    "ishares-bitcoin-trust-etf/latest-holdings.csv"
)
SOURCE = "blackrock_ibit_holdings"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 ORACLE-X/1.0"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def unix(value: datetime) -> int:
    return int(value.timestamp())


def number(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def ensure_schema(con) -> None:
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS institutional_flow_observations_v1 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            instrument TEXT NOT NULL,
            reference_date TEXT NOT NULL,
            reference_timestamp_unix INTEGER NOT NULL,
            available_at TEXT NOT NULL,
            available_at_unix INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            observed_at_unix INTEGER NOT NULL,
            shares_outstanding REAL,
            holdings_btc REAL,
            market_value_usd REAL,
            holdings_change_btc REAL,
            shares_change REAL,
            flow_proxy_usd REAL,
            revision INTEGER NOT NULL DEFAULT 1,
            fingerprint TEXT NOT NULL,
            data_kind TEXT NOT NULL,
            quality TEXT NOT NULL,
            raw_json TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source, instrument, reference_date, revision)
        );

        CREATE INDEX IF NOT EXISTS idx_institutional_flow_v1_available
        ON institutional_flow_observations_v1(available_at_unix, source, instrument);

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
    con.execute(
        """
        INSERT INTO global_source_registry (
            source, category, authority, endpoint, trust_tier, enabled,
            trading_authority, historical_research_authority, state, notes
        ) VALUES (?, 'INSTITUTIONAL', 'BlackRock iShares', ?, 'ISSUER_PRIMARY',
                  1, 0, 0, 'SHADOW_CONTEXT_ONLY',
                  'Official IBIT daily holdings; holdings change is a proxy, not reported net flow')
        ON CONFLICT(source) DO UPDATE SET
            authority=excluded.authority, endpoint=excluded.endpoint,
            trust_tier=excluded.trust_tier, enabled=excluded.enabled,
            trading_authority=0, historical_research_authority=0,
            state=excluded.state, notes=excluded.notes
        """,
        (SOURCE, IBIT_URL),
    )
    con.execute(
        """
        INSERT INTO global_source_registry (
            source, category, authority, endpoint, trust_tier, enabled,
            trading_authority, historical_research_authority, state, notes
        ) VALUES ('cme_positioning', 'INSTITUTIONAL', 'CME Group', NULL,
                  'OFFICIAL_LICENSED', 0, 0, 0, 'PENDING_LICENSE',
                  'Direct CME daily market data requires authorized/licensed access')
        ON CONFLICT(source) DO UPDATE SET
            trading_authority=0, historical_research_authority=0,
            state='PENDING_LICENSE', notes=excluded.notes
        """
    )
    con.commit()


def update_health(con, status: str, seen: int, changed: int, error: Optional[str]) -> None:
    now = utc_now()
    success = status == "ACTIVE"
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
        (
            SOURCE, status, iso(now), unix(now),
            iso(now) if success else None, unix(now) if success else None,
            seen, changed, error,
        ),
    )
    con.commit()


def parse_ibit_csv(raw: bytes) -> Dict[str, Any]:
    text = raw.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text)))
    reference_date = None
    shares = None
    header_index = None
    for index, row in enumerate(rows):
        if not row:
            continue
        label = row[0].strip()
        if label == "Fund Holdings as of" and len(row) > 1:
            reference_date = datetime.strptime(row[1].strip(), "%b %d, %Y").date()
        elif label == "Shares Outstanding" and len(row) > 1:
            shares = number(row[1])
        elif label == "Ticker":
            header_index = index
            break
    if reference_date is None or header_index is None:
        raise ValueError("IBIT CSV metadata/header not found")
    headers = [item.strip() for item in rows[header_index]]
    holdings = None
    market_value = None
    for row in rows[header_index + 1:]:
        if not row or row[0].strip().upper() != "BTC":
            continue
        item = dict(zip(headers, row))
        holdings = number(item.get("Quantity"))
        market_value = number(item.get("Market Value"))
        break
    if holdings is None or holdings <= 0 or market_value is None or market_value <= 0:
        raise ValueError("Valid BTC holding not found in IBIT CSV")
    return {
        "reference_date": reference_date.isoformat(),
        "reference_timestamp_unix": int(datetime.combine(reference_date, datetime.min.time(), tzinfo=timezone.utc).timestamp()),
        "shares_outstanding": shares,
        "holdings_btc": holdings,
        "market_value_usd": market_value,
    }


def fetch() -> bytes:
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
    )
    common_headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": IBIT_PAGE_URL,
        "Cache-Control": "no-cache",
    }
    # Establish the same first-party session used by the public download link.
    try:
        page_request = urllib.request.Request(
            IBIT_PAGE_URL,
            headers={**common_headers, "Accept": "text/html,*/*"},
        )
        with opener.open(page_request, timeout=30) as response:
            response.read(1024)
    except (OSError, urllib.error.URLError):
        # The public CSV endpoint can still work without the warm-up request.
        pass

    failures = []
    for url in (IBIT_URL, IBIT_FALLBACK_URL):
        try:
            request = urllib.request.Request(
                url,
                headers={**common_headers, "Accept": "text/csv,text/plain,*/*"},
            )
            with opener.open(request, timeout=30) as response:
                raw = response.read()
            if b"Fund Holdings as of" not in raw or b"Ticker,Name" not in raw:
                raise ValueError("response is not an IBIT holdings CSV")
            return raw
        except Exception as exc:
            failures.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(failures))


def store(con, item: Dict[str, Any], observed: datetime, raw: bytes) -> bool:
    fingerprint = hashlib.sha256(json.dumps(item, sort_keys=True).encode()).hexdigest()
    previous_same = con.execute(
        """
        SELECT revision, fingerprint FROM institutional_flow_observations_v1
        WHERE source=? AND instrument='IBIT' AND reference_date=?
        ORDER BY revision DESC LIMIT 1
        """,
        (SOURCE, item["reference_date"]),
    ).fetchone()
    if previous_same and previous_same["fingerprint"] == fingerprint:
        return False
    previous = con.execute(
        """
        SELECT shares_outstanding, holdings_btc
        FROM institutional_flow_observations_v1
        WHERE source=? AND instrument='IBIT' AND reference_date < ?
        ORDER BY reference_date DESC, revision DESC LIMIT 1
        """,
        (SOURCE, item["reference_date"]),
    ).fetchone()
    revision = int(previous_same["revision"]) + 1 if previous_same else 1
    holdings_change = None
    shares_change = None
    flow_proxy = None
    if previous:
        holdings_change = item["holdings_btc"] - float(previous["holdings_btc"])
        if item["shares_outstanding"] is not None and previous["shares_outstanding"] is not None:
            shares_change = item["shares_outstanding"] - float(previous["shares_outstanding"])
        btc_price_proxy = item["market_value_usd"] / item["holdings_btc"]
        flow_proxy = holdings_change * btc_price_proxy
    con.execute(
        """
        INSERT INTO institutional_flow_observations_v1 (
            source, instrument, reference_date, reference_timestamp_unix,
            available_at, available_at_unix, observed_at, observed_at_unix,
            shares_outstanding, holdings_btc, market_value_usd,
            holdings_change_btc, shares_change, flow_proxy_usd,
            revision, fingerprint, data_kind, quality, raw_json, created_at
        ) VALUES (?, 'IBIT', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            SOURCE, item["reference_date"], item["reference_timestamp_unix"],
            iso(observed), unix(observed), iso(observed), unix(observed),
            item["shares_outstanding"], item["holdings_btc"], item["market_value_usd"],
            holdings_change, shares_change, flow_proxy, revision, fingerprint,
            "OFFICIAL_ISSUER_HOLDINGS_OBSERVED_LIVE",
            "HOLDINGS_CHANGE_PROXY_NOT_REPORTED_NET_FLOW",
            json.dumps({"parsed": item, "raw_sha256": hashlib.sha256(raw).hexdigest()}),
            iso(observed),
        ),
    )
    con.commit()
    return True


def run() -> Dict[str, Any]:
    # Keep the CSV parser importable during release preflight, before this file
    # is installed into the ORACLE X project where database.db is available.
    from database.db import connect

    con = connect()
    try:
        ensure_schema(con)
        try:
            raw = fetch()
            item = parse_ibit_csv(raw)
            changed = int(store(con, item, utc_now(), raw))
            update_health(con, "ACTIVE", 1, changed, None)
            return {"status": "ACTIVE", "source": SOURCE, "changed": changed, **item}
        except Exception as exc:
            update_health(con, "DEGRADED", 0, 0, str(exc)[:500])
            return {"status": "DEGRADED", "source": SOURCE, "error": str(exc)}
    finally:
        con.close()


def main() -> None:
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    if result["status"] != "ACTIVE":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
