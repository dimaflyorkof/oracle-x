from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional

from database.db import connect


SYMBOL = "BTC"
REPORT_PATH = Path("global_intelligence_readiness.json")

SOURCE_REQUIREMENTS = {
    "price": {
        "table": "market_snapshots",
        "minimum_research_days": 730,
        "minimum_context_days": 90,
    },
    "orderflow": {
        "table": "orderflow_history",
        "minimum_research_days": 730,
        "minimum_context_days": 90,
    },
    "derivatives": {
        "table": "derivatives_history",
        "minimum_research_days": 365,
        "minimum_context_days": 90,
    },
    "macro": {
        "table": "macro_history",
        "minimum_research_days": 730,
        "minimum_context_days": 180,
    },
    "onchain": {
        "table": "onchain_history",
        "minimum_research_days": 730,
        "minimum_context_days": 180,
    },
    "sentiment": {
        "table": "sentiment_history",
        "minimum_research_days": 730,
        "minimum_context_days": 180,
    },
    "liquidations": {
        "table": "liquidation_history",
        "minimum_research_days": 180,
        "minimum_context_days": 30,
    },
    "institutional": {
        "table": "institutional_history",
        "minimum_research_days": 365,
        "minimum_context_days": 90,
    },
}

EXPECTED_GLOBAL_SOURCES = {
    "spot_exchanges": ["binance", "coinbase", "kraken"],
    "derivatives_exchanges": ["binance", "bybit", "okx", "cme"],
    "macro": ["fred_alfred", "official_release_calendar"],
    "institutional": ["btc_etf_flows", "cftc_cot", "cme_positioning"],
    "events": ["fed", "bls", "sec", "verified_news_feed"],
}


def iso(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def table_exists(con, table: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def columns(con, table: str) -> set[str]:
    return {
        str(row["name"])
        for row in con.execute(f"PRAGMA table_info({table})").fetchall()
    }


def scalar(con, sql: str, params: Iterable = ()):
    row = con.execute(sql, tuple(params)).fetchone()
    return row[0] if row else None


def coverage_status(days: float, research_days: int, context_days: int) -> str:
    if days >= research_days:
        return "RESEARCH_READY"
    if days >= context_days:
        return "CONTEXT_READY"
    if days > 0:
        return "ACCUMULATING"
    return "UNAVAILABLE"


def summarize_table(con, name: str, spec: Dict) -> Dict:
    table = spec["table"]
    if not table_exists(con, table):
        return {
            "table": table,
            "status": "MISSING_TABLE",
            "rows": 0,
            "days": 0.0,
        }

    available_columns = columns(con, table)
    timestamp_column = (
        "event_timestamp_unix"
        if "event_timestamp_unix" in available_columns
        else "timestamp_unix"
    )
    where = []
    params = []
    if "symbol" in available_columns and name not in {"macro", "sentiment"}:
        where.append("(symbol = ? OR symbol IS NULL)")
        params.append(SYMBOL)
    where_sql = " WHERE " + " AND ".join(where) if where else ""
    row = con.execute(
        f"SELECT COUNT(*) AS rows, MIN({timestamp_column}) AS first_ts, "
        f"MAX({timestamp_column}) AS last_ts FROM {table}{where_sql}",
        tuple(params),
    ).fetchone()
    count = int(row["rows"] or 0)
    first_ts = int(row["first_ts"]) if row["first_ts"] is not None else None
    last_ts = int(row["last_ts"]) if row["last_ts"] is not None else None
    days = (
        max(0.0, (last_ts - first_ts) / 86400.0)
        if first_ts is not None and last_ts is not None
        else 0.0
    )
    causal_contract = {
        "event_timestamp": "event_timestamp_unix" in available_columns,
        "available_at": "available_at_unix" in available_columns,
        "data_kind": "data_kind" in available_columns,
        "interval": "data_interval_seconds" in available_columns,
    }
    source_rows = []
    if "source" in available_columns:
        source_rows = [
            {
                "source": item["source"],
                "rows": int(item["rows"]),
                "first": iso(item["first_ts"]),
                "last": iso(item["last_ts"]),
            }
            for item in con.execute(
                f"SELECT source, COUNT(*) AS rows, "
                f"MIN({timestamp_column}) AS first_ts, "
                f"MAX({timestamp_column}) AS last_ts "
                f"FROM {table}{where_sql} GROUP BY source ORDER BY rows DESC",
                tuple(params),
            ).fetchall()
        ]

    result = {
        "table": table,
        "status": coverage_status(
            days,
            int(spec["minimum_research_days"]),
            int(spec["minimum_context_days"]),
        ),
        "rows": count,
        "days": round(days, 2),
        "first": iso(first_ts),
        "last": iso(last_ts),
        "minimum_context_days": int(spec["minimum_context_days"]),
        "minimum_research_days": int(spec["minimum_research_days"]),
        "causal_contract": causal_contract,
        "sources": source_rows,
    }

    if name == "price" and "timeframe" in available_columns:
        result["timeframes"] = {
            str(item["timeframe"]): int(item["rows"])
            for item in con.execute(
                "SELECT timeframe, COUNT(*) AS rows FROM market_snapshots "
                "WHERE symbol = ? GROUP BY timeframe ORDER BY timeframe",
                (SYMBOL,),
            ).fetchall()
        }
    return result


def recommendations(components: Dict[str, Dict]) -> list[str]:
    result = []
    if components["price"]["status"] != "RESEARCH_READY":
        result.append("Complete at least two years of gap-free 15m/1h/4h price history.")
    if components["orderflow"]["status"] != "RESEARCH_READY":
        result.append("Complete two years of causal spot and futures order-flow aggregates.")
    result.append("Add Coinbase and Kraken spot flow as independent confirmation sources.")
    result.append("Add Bybit and OKX futures flow with the same close-time availability contract.")
    if components["derivatives"]["status"] != "RESEARCH_READY":
        result.append("Keep derivatives context-only until at least one causal year is available.")
    if components["macro"]["status"] != "RESEARCH_READY":
        result.append("Backfill macro using ALFRED vintages and exact publication timestamps.")
    if components["institutional"]["status"] != "RESEARCH_READY":
        result.append("Add ETF, CME and CFTC data with publication-time metadata.")
    result.append("Add a verified event calendar before using news as a trading gate.")
    result.append("Run Global Intelligence V1 as shadow/context only before any capital deployment.")
    return result


def main() -> None:
    con = connect()
    try:
        components = {
            name: summarize_table(con, name, spec)
            for name, spec in SOURCE_REQUIREMENTS.items()
        }
    finally:
        con.close()

    research_ready = sorted(
        name for name, value in components.items()
        if value["status"] == "RESEARCH_READY"
    )
    context_ready = sorted(
        name for name, value in components.items()
        if value["status"] in {"RESEARCH_READY", "CONTEXT_READY"}
    )
    report = {
        "status": (
            "BASE_READY_GLOBAL_INCOMPLETE"
            if {"price", "orderflow"}.issubset(research_ready)
            else "BASE_INCOMPLETE"
        ),
        "symbol": SYMBOL,
        "generated_at": iso(int(time.time())),
        "policy": {
            "missing_data": "inactive_not_zero",
            "historical_visibility": "event_and_available_at",
            "deployment": "shadow_only",
        },
        "research_ready": research_ready,
        "context_ready": context_ready,
        "components": components,
        "expected_global_sources": EXPECTED_GLOBAL_SOURCES,
        "recommendations": recommendations(components),
    }
    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"Report: {REPORT_PATH}")


if __name__ == "__main__":
    main()
