from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional

from database.db import connect


VERSION = "INSTITUTIONAL-INTELLIGENCE-V1"


def clamp(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def robust_z(values: List[float]) -> float:
    if len(values) < 8:
        return 0.0
    center = statistics.median(values)
    mad = statistics.median(abs(value - center) for value in values)
    return (values[-1] - center) / (1.4826 * mad) if mad > 1e-12 else 0.0


@dataclass
class InstitutionalResult:
    version: str
    symbol: str
    as_of_unix: int
    status: str
    state: str
    score: float
    confidence: float
    data_coverage: float
    components: Dict[str, Any]
    unavailable: Dict[str, str]
    policy: Dict[str, Any]


def score_cftc(as_of: int) -> Dict[str, Any]:
    con = connect()
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='global_positioning'").fetchone():
            return {"status": "NO_TABLE", "active": False, "score": 0.0, "confidence": 0.0}
        rows = con.execute(
            """
            SELECT p.* FROM global_positioning p
            JOIN (
                SELECT report_date, MAX(revision) AS revision
                FROM global_positioning
                WHERE source='cftc_cot' AND contract_code='133741'
                  AND available_at_unix <= ?
                GROUP BY report_date
            ) latest ON latest.report_date=p.report_date AND latest.revision=p.revision
            WHERE p.source='cftc_cot' AND p.contract_code='133741'
              AND p.available_at_unix <= ?
            ORDER BY p.report_timestamp_unix DESC LIMIT 156
            """,
            (as_of, as_of),
        ).fetchall()
    finally:
        con.close()
    rows = list(reversed(rows))
    valid = [row for row in rows if row["open_interest"] and row["noncommercial_net"] is not None]
    if len(valid) < 26:
        return {"status": "ACCUMULATING", "active": False, "score": 0.0, "confidence": 0.0, "rows": len(valid)}
    ratios = [float(row["noncommercial_net"]) / float(row["open_interest"]) for row in valid]
    changes = [ratios[index] - ratios[index - 1] for index in range(1, len(ratios))]
    level_signal = math.tanh(robust_z(ratios) / 2.5)
    change_signal = math.tanh(robust_z(changes) / 2.5)
    score = clamp(0.60 * level_signal + 0.40 * change_signal)
    latest_available = int(valid[-1]["available_at_unix"])
    age = max(0, as_of - latest_available)
    freshness = 1.0 if age <= 10 * 86400 else 0.5 if age <= 17 * 86400 else 0.0
    return {
        "status": "ACTIVE" if freshness else "STALE",
        "active": bool(freshness),
        "score": round(score, 6),
        "confidence": round(0.70 * freshness, 6),
        "rows": len(valid),
        "latest_report_date": str(valid[-1]["report_date"]),
        "latest_available_unix": latest_available,
        "age_seconds": age,
        "noncommercial_net": float(valid[-1]["noncommercial_net"]),
        "open_interest": float(valid[-1]["open_interest"]),
        "net_ratio": round(ratios[-1], 6),
        "level_causal_z": round(robust_z(ratios), 6),
        "change_causal_z": round(robust_z(changes), 6),
    }


def score_ibit(as_of: int) -> Dict[str, Any]:
    con = connect()
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='institutional_flow_observations_v1'").fetchone():
            return {"status": "NO_TABLE", "active": False, "score": 0.0, "confidence": 0.0}
        rows = con.execute(
            """
            SELECT * FROM institutional_flow_observations_v1
            WHERE source='blackrock_ibit_holdings' AND instrument='IBIT'
              AND available_at_unix <= ?
            ORDER BY reference_date DESC, revision DESC LIMIT 30
            """,
            (as_of,),
        ).fetchall()
    finally:
        con.close()
    unique = {}
    for row in rows:
        unique.setdefault(str(row["reference_date"]), row)
    values = list(reversed([unique[key] for key in sorted(unique, reverse=True)]))
    flows = [float(row["flow_proxy_usd"]) for row in values if row["flow_proxy_usd"] is not None]
    if len(flows) < 5:
        return {"status": "ACCUMULATING", "active": False, "score": 0.0, "confidence": 0.0, "rows": len(values), "required_flow_days": 5}
    recent = sum(flows[-5:])
    scale = statistics.median(abs(value) for value in flows) or 1.0
    score = clamp(math.tanh(recent / (scale * 5.0)))
    latest = values[-1]
    age = max(0, as_of - int(latest["available_at_unix"]))
    freshness = 1.0 if age <= 4 * 86400 else 0.0
    return {
        "status": "ACTIVE" if freshness else "STALE",
        "active": bool(freshness),
        "score": round(score, 6),
        "confidence": round(min(0.75, len(flows) / 20.0) * freshness, 6),
        "rows": len(values),
        "latest_reference_date": str(latest["reference_date"]),
        "latest_available_unix": int(latest["available_at_unix"]),
        "holdings_btc": float(latest["holdings_btc"]),
        "shares_outstanding": float(latest["shares_outstanding"]) if latest["shares_outstanding"] is not None else None,
        "flow_proxy_5d_usd": round(recent, 2),
        "quality": "HOLDINGS_CHANGE_PROXY_NOT_REPORTED_NET_FLOW",
    }


def analyze_institutional_intelligence(as_of_ts: Optional[int] = None) -> InstitutionalResult:
    as_of = int(as_of_ts if as_of_ts is not None else time.time())
    components = {"cftc_cme_btc": score_cftc(as_of), "ibit_holdings": score_ibit(as_of)}
    weights = {"cftc_cme_btc": 0.55, "ibit_holdings": 0.45}
    active = [(components[name]["score"], weights[name] * components[name]["confidence"]) for name in components if components[name].get("active")]
    denominator = sum(weight for _, weight in active)
    score = sum(value * weight for value, weight in active) / denominator if denominator else 0.0
    coverage = sum(weights[name] for name in components if components[name].get("active"))
    confidence = denominator / coverage if coverage else 0.0
    if not active:
        state, status = "COLLECT", "ACCUMULATING"
    elif score >= 0.25:
        state, status = "INSTITUTIONAL_ACCUMULATION", "ACTIVE"
    elif score <= -0.25:
        state, status = "INSTITUTIONAL_DISTRIBUTION", "ACTIVE"
    else:
        state, status = "INSTITUTIONAL_NEUTRAL", "ACTIVE"
    unavailable = {name: item["status"] for name, item in components.items() if not item.get("active")}
    return InstitutionalResult(
        VERSION, "BTC", as_of, status, state, round(score, 6), round(confidence, 6),
        round(coverage, 6), components, unavailable,
        {
            "deployment": "SHADOW_CONTEXT_ONLY",
            "trading_authority": False,
            "historical_backtest_authority": False,
            "cftc_visibility": "AVAILABLE_AT_UNIX_LE_AS_OF",
            "etf_visibility": "OBSERVED_AT_AS_AVAILABLE_AT",
            "etf_metric": "HOLDINGS_CHANGE_PROXY_NOT_REPORTED_NET_FLOW",
            "direct_cme": "PENDING_AUTHORIZED_LICENSE",
            "missing_data": "INACTIVE_NOT_ZERO",
        },
    )


def main() -> None:
    print("ORACLE X — INSTITUTIONAL INTELLIGENCE V1")
    print(json.dumps(asdict(analyze_institutional_intelligence()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
