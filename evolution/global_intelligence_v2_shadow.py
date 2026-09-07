from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Dict

from core.global_intelligence_v2 import analyze_global_intelligence_v2
from database.db import connect


SYMBOL = "BTC"
POLL_SECONDS = 300


def iso(timestamp_unix: int) -> str:
    return datetime.fromtimestamp(
        int(timestamp_unix),
        tz=timezone.utc,
    ).isoformat()


def init_schema() -> None:
    con = connect()
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS global_intelligence_v2_snapshots (
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
                raw_json TEXT NOT NULL
            )
            """
        )
        con.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_global_intelligence_v2_time
            ON global_intelligence_v2_snapshots (
                symbol,
                generated_unix
            )
            """
        )
        con.commit()
    finally:
        con.close()


def save_snapshot(result) -> int:
    generated_unix = int(time.time())
    bucket = generated_unix // POLL_SECONDS * POLL_SECONDS
    raw_json = json.dumps(
        asdict(result),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO global_intelligence_v2_snapshots (
                generated_timestamp, generated_unix,
                as_of_bucket_unix, symbol, version,
                decision, market_state, flow_state,
                spot_consensus, score, confidence,
                data_coverage, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(as_of_bucket_unix) DO UPDATE SET
                generated_timestamp = excluded.generated_timestamp,
                generated_unix = excluded.generated_unix,
                symbol = excluded.symbol,
                version = excluded.version,
                decision = excluded.decision,
                market_state = excluded.market_state,
                flow_state = excluded.flow_state,
                spot_consensus = excluded.spot_consensus,
                score = excluded.score,
                confidence = excluded.confidence,
                data_coverage = excluded.data_coverage,
                raw_json = excluded.raw_json
            """,
            (
                iso(generated_unix),
                generated_unix,
                bucket,
                result.symbol,
                result.version,
                result.decision,
                result.market_state,
                result.flow_state,
                result.spot_consensus,
                result.score,
                result.confidence,
                result.data_coverage,
                raw_json,
            ),
        )
        con.commit()
        row = con.execute(
            "SELECT id FROM global_intelligence_v2_snapshots "
            "WHERE as_of_bucket_unix = ?",
            (bucket,),
        ).fetchone()
        return int(row["id"])
    finally:
        con.close()


def run_once() -> Dict:
    init_schema()
    result = analyze_global_intelligence_v2(SYMBOL)
    snapshot_id = save_snapshot(result)
    output = {
        "snapshot_id": snapshot_id,
        "generated_timestamp": iso(int(time.time())),
        **asdict(result),
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return output


def run_forever() -> None:
    print("ORACLE X — GLOBAL INTELLIGENCE V2 SHADOW", flush=True)
    print("Mode: context and A/B evidence only; no capital", flush=True)
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception as exc:
            print(
                json.dumps(
                    {
                        "generated_timestamp": iso(int(time.time())),
                        "action": "ERROR",
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, POLL_SECONDS - elapsed))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.once:
        run_once()
    else:
        try:
            run_forever()
        except KeyboardInterrupt:
            print("Global Intelligence V2 stopped", flush=True)


if __name__ == "__main__":
    main()
