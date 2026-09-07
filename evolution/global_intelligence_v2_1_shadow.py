from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict

from core.global_intelligence_v2_1 import analyze_global_intelligence_v2_1
from database.db import connect


SYMBOL = "BTC"
POLL_SECONDS = 300


def iso(timestamp_unix: int) -> str:
    return datetime.fromtimestamp(int(timestamp_unix), tz=timezone.utc).isoformat()


def init_schema() -> None:
    con = connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS global_intelligence_v2_1_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_timestamp TEXT NOT NULL,
                generated_unix INTEGER NOT NULL,
                as_of_bucket_unix INTEGER NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                version TEXT NOT NULL,
                decision TEXT NOT NULL,
                market_state TEXT NOT NULL,
                flow_state TEXT NOT NULL,
                spot_consensus TEXT NOT NULL,
                score REAL NOT NULL,
                confidence REAL NOT NULL,
                data_coverage REAL NOT NULL,
                macro_score REAL,
                macro_confidence REAL,
                macro_active_series INTEGER,
                base_v2_decision TEXT,
                raw_json TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_global_intelligence_v2_1_time "
            "ON global_intelligence_v2_1_snapshots(symbol, generated_unix)"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS global_intelligence_v2_1_trade_ab (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shadow_trade_id INTEGER NOT NULL UNIQUE,
                signal_candle_unix INTEGER NOT NULL,
                decision_unix INTEGER NOT NULL,
                side TEXT NOT NULL,
                v2_1_decision TEXT NOT NULL,
                v2_1_score REAL NOT NULL,
                v2_1_confidence REAL NOT NULL,
                v2_1_coverage REAL NOT NULL,
                v2_1_allowed INTEGER NOT NULL,
                macro_score REAL,
                macro_confidence REAL,
                trade_status TEXT NOT NULL,
                result_r REAL,
                evaluated_timestamp TEXT NOT NULL,
                evaluated_unix INTEGER NOT NULL,
                raw_json TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE INDEX IF NOT EXISTS idx_global_intelligence_v2_1_ab_status "
            "ON global_intelligence_v2_1_trade_ab(trade_status, v2_1_allowed)"
        )
        con.commit()
    finally:
        con.close()


def save_snapshot(result) -> int:
    generated = int(time.time())
    bucket = generated // POLL_SECONDS * POLL_SECONDS
    macro = result.macro_vintage
    raw = json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":"))
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO global_intelligence_v2_1_snapshots (
                generated_timestamp, generated_unix, as_of_bucket_unix,
                symbol, version, decision, market_state, flow_state,
                spot_consensus, score, confidence, data_coverage,
                macro_score, macro_confidence, macro_active_series,
                base_v2_decision, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(as_of_bucket_unix) DO UPDATE SET
                generated_timestamp=excluded.generated_timestamp,
                generated_unix=excluded.generated_unix,
                decision=excluded.decision,
                market_state=excluded.market_state,
                flow_state=excluded.flow_state,
                spot_consensus=excluded.spot_consensus,
                score=excluded.score,
                confidence=excluded.confidence,
                data_coverage=excluded.data_coverage,
                macro_score=excluded.macro_score,
                macro_confidence=excluded.macro_confidence,
                macro_active_series=excluded.macro_active_series,
                base_v2_decision=excluded.base_v2_decision,
                raw_json=excluded.raw_json
            """,
            (
                iso(generated), generated, bucket, result.symbol, result.version,
                result.decision, result.market_state, result.flow_state,
                result.spot_consensus, result.score, result.confidence,
                result.data_coverage, macro.get("score"), macro.get("confidence"),
                macro.get("active_series"), result.base_v2.get("decision"), raw,
            ),
        )
        con.commit()
        row = con.execute(
            "SELECT id FROM global_intelligence_v2_1_snapshots WHERE as_of_bucket_unix=?",
            (bucket,),
        ).fetchone()
        return int(row["id"])
    finally:
        con.close()


def sync_trade_ab() -> Dict:
    con = connect()
    try:
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_v5_1_trades'"
        ).fetchone()
        if not exists:
            return {"evaluated": 0, "updated": 0, "reason": "NO_V5_1_TABLE"}
        rows = con.execute(
            """
            SELECT id, signal_candle_unix, entry_candle_unix, side, status, result_r
            FROM shadow_v5_1_trades
            ORDER BY id
            """
        ).fetchall()
        known = {
            int(row["shadow_trade_id"]): dict(row)
            for row in con.execute(
                "SELECT shadow_trade_id, trade_status, result_r "
                "FROM global_intelligence_v2_1_trade_ab"
            ).fetchall()
        }
    finally:
        con.close()
    evaluated = 0
    updated = 0
    for row in rows:
        trade = dict(row)
        trade_id = int(trade["id"])
        existing = known.get(trade_id)
        if existing is None:
            decision_unix = int(trade["entry_candle_unix"])
            result = analyze_global_intelligence_v2_1(SYMBOL, as_of_ts=decision_unix)
            required = "LONG_ALLOWED" if trade["side"] == "LONG" else "SHORT_ALLOWED"
            allowed = result.data_coverage >= 0.55 and result.decision == required
            macro = result.macro_vintage
            now = int(time.time())
            con = connect()
            try:
                con.execute(
                    """
                    INSERT OR IGNORE INTO global_intelligence_v2_1_trade_ab (
                        shadow_trade_id, signal_candle_unix, decision_unix, side,
                        v2_1_decision, v2_1_score, v2_1_confidence,
                        v2_1_coverage, v2_1_allowed, macro_score,
                        macro_confidence, trade_status, result_r,
                        evaluated_timestamp, evaluated_unix, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        trade_id, int(trade["signal_candle_unix"]), decision_unix,
                        trade["side"], result.decision, result.score,
                        result.confidence, result.data_coverage, int(allowed),
                        macro.get("score"), macro.get("confidence"), trade["status"],
                        trade["result_r"], iso(now), now,
                        json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                con.commit()
            finally:
                con.close()
            evaluated += 1
        elif existing["trade_status"] != trade["status"] or existing["result_r"] != trade["result_r"]:
            con = connect()
            try:
                con.execute(
                    "UPDATE global_intelligence_v2_1_trade_ab SET trade_status=?, result_r=? "
                    "WHERE shadow_trade_id=?",
                    (trade["status"], trade["result_r"], trade_id),
                )
                con.commit()
            finally:
                con.close()
            updated += 1
    return {"evaluated": evaluated, "updated": updated}


def ab_summary() -> Dict:
    con = connect()
    try:
        rows = con.execute(
            "SELECT v2_1_allowed, result_r FROM global_intelligence_v2_1_trade_ab "
            "WHERE trade_status='CLOSED' AND result_r IS NOT NULL ORDER BY id"
        ).fetchall()
    finally:
        con.close()
    baseline = [float(row["result_r"]) for row in rows]
    overlay = [
        float(row["result_r"]) for row in rows if int(row["v2_1_allowed"]) == 1
    ]
    return {
        "closed_baseline_trades": len(baseline),
        "baseline_total_r": round(sum(baseline), 6),
        "v2_1_allowed_trades": len(overlay),
        "v2_1_overlay_total_r": round(sum(overlay), 6),
    }


def run_once() -> Dict:
    init_schema()
    result = analyze_global_intelligence_v2_1(SYMBOL)
    output = {
        "snapshot_id": save_snapshot(result),
        "trade_ab_sync": sync_trade_ab(),
        "trade_ab_summary": ab_summary(),
        **asdict(result),
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return output


def run_forever() -> None:
    print("ORACLE X — GLOBAL INTELLIGENCE V2.1 SHADOW", flush=True)
    print("Mode: forward A/B context only; no trading authority", flush=True)
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception as exc:
            print(json.dumps({"action": "ERROR", "error": f"{type(exc).__name__}: {exc}"}), flush=True)
        time.sleep(max(1.0, POLL_SECONDS - (time.monotonic() - started)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    run_once() if args.once else run_forever()


if __name__ == "__main__":
    main()
