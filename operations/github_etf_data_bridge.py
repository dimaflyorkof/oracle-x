from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional


VERSION = "ORACLE-X-GITHUB-ETF-BRIDGE-V1"
OUTPUT = Path("data/institutional/btc_etf_daily.json")
BLACKROCK_URLS = (
    "https://www.ishares.com/us/products/333011/fund/1467271812596.ajax"
    "?dataType=fund&fileName=IBIT_holdings&fileType=csv",
    "https://www.ishares.com/us/products/333011/"
    "ishares-bitcoin-trust-etf/latest-holdings.csv",
)
FARSIDE_URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 ORACLE-X-DATA-BRIDGE/1.0"
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def fetch(url: str, accept: str) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=45) as response:
        return response.read()


def number(value: Any) -> Optional[float]:
    text = str(value or "").strip().replace(",", "").replace("$", "")
    if not text or text in {"-", "–", "—", "N/A"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        result = float(text)
    except ValueError:
        return None
    return -result if negative else result


def parse_blackrock(raw: bytes) -> Dict[str, Any]:
    text = raw.decode("utf-8-sig", errors="replace")
    if "Fund Holdings as of" not in text or "Ticker,Name" not in text:
        raise ValueError("response is not an IBIT holdings CSV")
    holdings_date = None
    shares = None
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        key = row[0].strip()
        if key == "Fund Holdings as of" and len(row) > 1:
            holdings_date = datetime.strptime(row[1].strip(), "%b %d, %Y").date()
        elif key == "Shares Outstanding" and len(row) > 1:
            shares = number(row[1])
    header_index = text.find("Ticker,Name")
    rows = list(csv.DictReader(io.StringIO(text[header_index:])))
    btc = next((row for row in rows if row.get("Ticker", "").strip('" ') == "BTC"), None)
    if not holdings_date or not btc:
        raise ValueError("IBIT date or BTC holding missing")
    quantity = number(btc.get("Quantity"))
    market_value = number(btc.get("Market Value"))
    if quantity is None or quantity <= 0 or market_value is None or market_value <= 0:
        raise ValueError("invalid IBIT holding values")
    return {
        "status": "ACTIVE",
        "authority": "BlackRock iShares",
        "trust_tier": "ISSUER_PRIMARY",
        "metric": "OFFICIAL_DAILY_HOLDINGS_NOT_NET_FLOW",
        "reference_date": holdings_date.isoformat(),
        "shares_outstanding": shares,
        "holdings_btc": quantity,
        "market_value_usd": market_value,
    }


class TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: List[List[List[str]]] = []
        self._table: Optional[List[List[str]]] = None
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"th", "td"} and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"th", "td"} and self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


def parse_farside(raw: bytes, today: Optional[date] = None) -> Dict[str, Any]:
    parser = TableParser()
    parser.feed(raw.decode("utf-8", errors="replace"))
    table = next(
        (
            table for table in parser.tables
            if table and "Date" in table[0] and "IBIT" in table[0] and "Total" in table[0]
        ),
        None,
    )
    if not table:
        raise ValueError("Farside BTC ETF table not found")
    headers = table[0]
    records = []
    for cells in table[1:]:
        if len(cells) < len(headers):
            cells += [""] * (len(headers) - len(cells))
        item = dict(zip(headers, cells))
        try:
            report_date = datetime.strptime(item["Date"], "%d %b %Y").date()
        except (KeyError, ValueError):
            continue
        flows = {
            key: number(item.get(key))
            for key in headers
            if key != "Date"
        }
        records.append({
            "date": report_date.isoformat(),
            "flows_usd_millions": flows,
            "quality": "COMPLETE",
        })
    records.sort(key=lambda value: value["date"])
    if not records:
        raise ValueError("Farside table contains no dated records")
    current = today or utc_now().date()
    latest_date = date.fromisoformat(records[-1]["date"])
    if (current - latest_date).days <= 2:
        records[-1]["quality"] = "RECENT_MAY_REVISE"
    return {
        "status": "ACTIVE",
        "authority": "Farside Investors",
        "trust_tier": "REGULATED_SECONDARY_AGGREGATOR",
        "metric": "REPORTED_DAILY_US_SPOT_BTC_ETF_NET_FLOWS_USD_MILLIONS",
        "missing_values": "NULL_NOT_ZERO",
        "first_date": records[0]["date"],
        "latest_date": records[-1]["date"],
        "records": records,
    }


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def load_existing() -> Optional[Dict[str, Any]]:
    if not OUTPUT.exists():
        return None
    try:
        return json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def main() -> None:
    sources: Dict[str, Any] = {}
    errors: Dict[str, List[str]] = {"blackrock_ibit": [], "farside_btc_etf_flows": []}
    for url in BLACKROCK_URLS:
        try:
            sources["blackrock_ibit"] = parse_blackrock(fetch(url, "text/csv,text/plain,*/*"))
            sources["blackrock_ibit"]["source_url"] = url
            break
        except Exception as exc:
            errors["blackrock_ibit"].append(str(exc))
    try:
        sources["farside_btc_etf_flows"] = parse_farside(fetch(FARSIDE_URL, "text/html,*/*"))
        sources["farside_btc_etf_flows"]["source_url"] = FARSIDE_URL
    except Exception as exc:
        errors["farside_btc_etf_flows"].append(str(exc))
    if not sources:
        raise SystemExit("No ETF source succeeded: " + json.dumps(errors))

    source_hash = hashlib.sha256(canonical(sources)).hexdigest()
    existing = load_existing()
    if existing and existing.get("source_data_sha256") == source_hash:
        print("ETF DATA UNCHANGED", source_hash)
        return
    document = {
        "version": VERSION,
        "generated_at": utc_now().isoformat(timespec="seconds"),
        "deployment": "DATA_BRIDGE_ONLY",
        "trading_authority": False,
        "historical_backtest_authority": False,
        "sources": sources,
        "refresh_errors": errors,
        "source_data_sha256": source_hash,
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print("ETF DATA UPDATED", source_hash, sorted(sources))


if __name__ == "__main__":
    main()
