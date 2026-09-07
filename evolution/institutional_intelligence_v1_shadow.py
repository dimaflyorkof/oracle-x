from __future__ import annotations

import argparse
import json
import time
from dataclasses import asdict
from datetime import datetime, timezone

from core.institutional_intelligence_v1 import analyze_institutional_intelligence
from database.db import connect


POLL_SECONDS = 300


def iso(value: int) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def init_schema() -> None:
    con = connect()
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS institutional_intelligence_v1_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generated_timestamp TEXT NOT NULL,
                generated_unix INTEGER NOT NULL,
                as_of_bucket_unix INTEGER NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                version TEXT NOT NULL,
                status TEXT NOT NULL,
                state TEXT NOT NULL,
                score REAL NOT NULL,
                confidence REAL NOT NULL,
                data_coverage REAL NOT NULL,
                raw_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_institutional_intelligence_v1_time
            ON institutional_intelligence_v1_snapshots(symbol, generated_unix);
            """
        )
        con.commit()
    finally:
        con.close()


def run_once():
    init_schema()
    result = analyze_institutional_intelligence()
    now = int(time.time())
    bucket = now // POLL_SECONDS * POLL_SECONDS
    raw = json.dumps(asdict(result), ensure_ascii=False, separators=(",", ":"))
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO institutional_intelligence_v1_snapshots (
                generated_timestamp, generated_unix, as_of_bucket_unix,
                symbol, version, status, state, score, confidence,
                data_coverage, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(as_of_bucket_unix) DO UPDATE SET
                generated_timestamp=excluded.generated_timestamp,
                generated_unix=excluded.generated_unix,
                status=excluded.status, state=excluded.state,
                score=excluded.score, confidence=excluded.confidence,
                data_coverage=excluded.data_coverage, raw_json=excluded.raw_json
            """,
            (iso(now), now, bucket, result.symbol, result.version, result.status,
             result.state, result.score, result.confidence, result.data_coverage, raw),
        )
        con.commit()
    finally:
        con.close()
    output = {"generated_unix": now, **asdict(result)}
    print(json.dumps(output, ensure_ascii=False), flush=True)
    return output


def run_forever() -> None:
    print("ORACLE X — INSTITUTIONAL INTELLIGENCE V1 SHADOW", flush=True)
    print("CFTC + official issuer holdings | no trading authority", flush=True)
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
