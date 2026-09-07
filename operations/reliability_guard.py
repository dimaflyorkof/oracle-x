from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


DB_PATH = Path(
    os.getenv("ORACLE_X_DB", "/root/oracle-x/database/oracle_x.db")
)
RUNTIME_DIR = Path(
    os.getenv("ORACLE_X_RUNTIME_DIR", "/root/oracle-x/runtime")
)
STATUS_FILE = RUNTIME_DIR / "reliability_guard_status.json"
STATE_FILE = RUNTIME_DIR / "reliability_guard_state.json"
BACKUP_STATUS_FILE = RUNTIME_DIR / "backup_status.json"
CHECK_SECONDS = int(os.getenv("ORACLE_X_GUARD_INTERVAL", "300"))
RESTART_COOLDOWN_SECONDS = 30 * 60
INTEGRITY_INTERVAL_SECONDS = 6 * 60 * 60
GAP_LOOKBACK_SECONDS = 48 * 60 * 60

SERVICE_NAMES = (
    "btc-oracle.service",
    "oracle-x-v5-1-shadow.service",
    "oracle-x-global-intelligence-v2.service",
    "oracle-x-global-spot-flow.service",
    "oracle-x-orderflow-kline.service",
    "oracle-ohlcv.service",
    "oracle-binance-derivatives.service",
    "oracle-orderflow.service",
    "oracle-liquidations.service",
    "oracle-macro.service",
    "oracle-onchain.service",
    "oracle-sentiment.service",
)

RESTARTABLE_SERVICES = {
    "global_spot": "oracle-x-global-spot-flow.service",
    "binance_klines": "oracle-x-orderflow-kline.service",
    "price": "oracle-ohlcv.service",
    "derivatives": "oracle-binance-derivatives.service",
    "liquidations": "oracle-liquidations.service",
    "macro": "oracle-macro.service",
    "onchain": "oracle-onchain.service",
    "sentiment": "oracle-sentiment.service",
}


def utc_iso(timestamp_unix: Optional[int] = None) -> str:
    value = int(timestamp_unix if timestamp_unix is not None else time.time())
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


def atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def db_connect() -> sqlite3.Connection:
    if not DB_PATH.is_file():
        raise RuntimeError(f"Database not found: {DB_PATH}")
    con = sqlite3.connect(
        f"file:{DB_PATH}?mode=ro", uri=True, timeout=15
    )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA query_only=ON")
    return con


def table_exists(con: sqlite3.Connection, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def latest_timestamp(
    con: sqlite3.Connection,
    table: str,
    expression: str,
    where: str,
) -> Optional[int]:
    if not table_exists(con, table):
        return None
    row = con.execute(
        f"SELECT MAX({expression}) AS ts FROM {table} WHERE {where}"
    ).fetchone()
    return int(row["ts"]) if row and row["ts"] is not None else None


def latest_complete_flow(
    con: sqlite3.Connection,
    source: str,
) -> Optional[int]:
    rows = con.execute(
        "SELECT timestamp_unix, raw_json FROM orderflow_history "
        "WHERE symbol='BTC' AND source=? "
        "ORDER BY timestamp_unix DESC, id DESC LIMIT 24",
        (source,),
    ).fetchall()
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if (
            raw.get("quality") == "COMPLETE"
            and not bool(raw.get("connection_interrupted"))
        ):
            return int(row["timestamp_unix"]) + 900
    return None


def age_check(
    timestamp_unix: Optional[int],
    max_age_seconds: int,
) -> Dict[str, Any]:
    if timestamp_unix is None:
        return {"status": "MISSING", "age_seconds": None}
    age = max(0, int(time.time()) - int(timestamp_unix))
    return {
        "status": "ACTIVE" if age <= max_age_seconds else "STALE",
        "age_seconds": age,
        "latest_unix": int(timestamp_unix),
    }


def count_gaps(
    con: sqlite3.Connection,
    table: str,
    where: str,
    interval_seconds: int,
) -> int:
    cutoff = int(time.time()) - GAP_LOOKBACK_SECONDS
    row = con.execute(
        f"""
        SELECT COUNT(*) AS gaps
        FROM (
            SELECT timestamp_unix,
                   LEAD(timestamp_unix) OVER (ORDER BY timestamp_unix) AS next_ts
            FROM {table}
            WHERE {where} AND timestamp_unix >= ?
            GROUP BY timestamp_unix
        )
        WHERE next_ts - timestamp_unix > ?
        """,
        (cutoff, interval_seconds),
    ).fetchone()
    return int(row["gaps"] or 0)


def service_state(service: str) -> str:
    result = subprocess.run(
        ["systemctl", "is-active", service],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return (result.stdout.strip() or "unknown").upper()


def database_checks(
    state: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    now = int(time.time())
    con = db_connect()
    try:
        data = {
            "price": age_check(
                latest_timestamp(
                    con,
                    "market_snapshots",
                    "timestamp_unix + 900",
                    "symbol='BTC' AND timeframe='15m'",
                ),
                2100,
            ),
            "binance_spot": age_check(
                latest_timestamp(
                    con,
                    "orderflow_history",
                    "timestamp_unix + 900",
                    "symbol='BTC' AND source='binance_spot_kline_15m'",
                ),
                2100,
            ),
            "binance_futures": age_check(
                latest_timestamp(
                    con,
                    "orderflow_history",
                    "timestamp_unix + 900",
                    "symbol='BTC' AND source='binance_futures_kline_15m'",
                ),
                2100,
            ),
            "coinbase": age_check(
                latest_complete_flow(con, "coinbase_spot_trades_15m"),
                2100,
            ),
            "kraken": age_check(
                latest_complete_flow(con, "kraken_spot_trades_15m"),
                2100,
            ),
            "derivatives": age_check(
                latest_timestamp(
                    con,
                    "derivatives_history",
                    "COALESCE(available_at_unix, timestamp_unix)",
                    "symbol='BTC'",
                ),
                1800,
            ),
            "liquidations": age_check(
                latest_timestamp(
                    con,
                    "liquidation_history",
                    "timestamp_unix",
                    "symbol='BTC'",
                ),
                1800,
            ),
            "global_v2": age_check(
                latest_timestamp(
                    con,
                    "global_intelligence_v2_snapshots",
                    "generated_unix",
                    "symbol='BTC'",
                ),
                1200,
            ),
            "macro": age_check(
                latest_timestamp(con, "macro_history", "timestamp_unix", "1=1"),
                3 * 86400,
            ),
            "onchain": age_check(
                latest_timestamp(
                    con,
                    "onchain_history",
                    "timestamp_unix",
                    "symbol='BTC'",
                ),
                3 * 86400,
            ),
            "sentiment": age_check(
                latest_timestamp(con, "sentiment_history", "timestamp_unix", "1=1"),
                3 * 86400,
            ),
        }
        gaps = {
            "market_15m": count_gaps(
                con,
                "market_snapshots",
                "symbol='BTC' AND timeframe='15m'",
                900,
            ),
            "market_1h": count_gaps(
                con,
                "market_snapshots",
                "symbol='BTC' AND timeframe='1h'",
                3600,
            ),
            "market_4h": count_gaps(
                con,
                "market_snapshots",
                "symbol='BTC' AND timeframe='4h'",
                14400,
            ),
            "binance_spot_15m": count_gaps(
                con,
                "orderflow_history",
                "symbol='BTC' AND source='binance_spot_kline_15m'",
                900,
            ),
            "binance_futures_15m": count_gaps(
                con,
                "orderflow_history",
                "symbol='BTC' AND source='binance_futures_kline_15m'",
                900,
            ),
        }
        last_integrity = int(state.get("last_integrity_unix") or 0)
        integrity = str(state.get("integrity") or "NOT_CHECKED")
        if now - last_integrity >= INTEGRITY_INTERVAL_SECONDS:
            row = con.execute("PRAGMA quick_check").fetchone()
            integrity = str(row[0]) if row else "NO_RESULT"
            state["last_integrity_unix"] = now
            state["integrity"] = integrity
        return data, {"integrity": integrity, "gaps": gaps}
    finally:
        con.close()


def restart_service(service: str) -> Dict[str, Any]:
    completed = subprocess.run(
        ["systemctl", "restart", service],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return {
        "service": service,
        "success": completed.returncode == 0,
        "returncode": completed.returncode,
        "timestamp_unix": int(time.time()),
    }


def maybe_repair(
    data: Dict[str, Any],
    services: Dict[str, str],
    state: Dict[str, Any],
) -> list[Dict[str, Any]]:
    failures = state.setdefault("consecutive_failures", {})
    cooldowns = state.setdefault("restart_unix", {})
    repairs = []
    repair_groups = {
        "price": ["price"],
        "binance_klines": ["binance_spot", "binance_futures"],
        "global_spot": ["coinbase", "kraken"],
        "derivatives": ["derivatives"],
        "liquidations": ["liquidations"],
        "macro": ["macro"],
        "onchain": ["onchain"],
        "sentiment": ["sentiment"],
    }
    now = int(time.time())
    for group, checks in repair_groups.items():
        service = RESTARTABLE_SERVICES[group]
        unhealthy = any(data[name]["status"] != "ACTIVE" for name in checks)
        inactive = services.get(service) != "ACTIVE"
        failures[group] = int(failures.get(group, 0)) + 1 if unhealthy or inactive else 0
        if failures[group] < 3:
            continue
        if now - int(cooldowns.get(group, 0)) < RESTART_COOLDOWN_SECONDS:
            continue
        repair = restart_service(service)
        repair["reason"] = "SERVICE_INACTIVE" if inactive else "DATA_STALE"
        repairs.append(repair)
        cooldowns[group] = now
        failures[group] = 0
    return repairs


def run_check() -> Dict[str, Any]:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    state = read_json(STATE_FILE)
    data, database = database_checks(state)
    services = {name: service_state(name) for name in SERVICE_NAMES}
    repairs = maybe_repair(data, services, state)

    disk = shutil.disk_usage(DB_PATH.parent)
    disk_percent = disk.used / disk.total * 100.0 if disk.total else 100.0
    backup = read_json(BACKUP_STATUS_FILE)
    backup_timestamp = int(
        backup.get("last_success_unix")
        or (backup.get("generated_unix") if backup.get("status") == "OK" else 0)
        or 0
    )
    backup_age = int(time.time()) - backup_timestamp if backup_timestamp else None
    backup_ok = (
        backup.get("status") in {"OK", "RUNNING"}
        and backup_age is not None
        and backup_age <= 36 * 3600
    )

    critical = []
    warnings = []
    if str(database["integrity"]).lower() != "ok":
        critical.append("DATABASE_INTEGRITY")
    if disk_percent >= 90.0:
        critical.append("DISK_USAGE")
    elif disk_percent >= 80.0:
        warnings.append("DISK_USAGE")
    if not backup_ok:
        warnings.append("BACKUP")
    for name, value in services.items():
        if value != "ACTIVE":
            warnings.append(f"SERVICE:{name}")
    for name, value in data.items():
        if value["status"] != "ACTIVE":
            warnings.append(f"DATA:{name}")
    for name, gaps in database["gaps"].items():
        if gaps:
            warnings.append(f"GAPS:{name}:{gaps}")

    overall = "CRITICAL" if critical else "DEGRADED" if warnings else "HEALTHY"
    result = {
        "version": "RELIABILITY-GUARD-V1",
        "status": overall,
        "generated_unix": int(time.time()),
        "generated_timestamp": utc_iso(),
        "database": {
            "path": str(DB_PATH),
            "bytes": DB_PATH.stat().st_size,
            "integrity": database["integrity"],
            "gaps": database["gaps"],
        },
        "disk": {
            "used_percent": round(disk_percent, 2),
            "free_bytes": disk.free,
        },
        "backup": {
            "status": backup.get("status", "MISSING"),
            "age_seconds": backup_age,
            "file": backup.get("file"),
        },
        "data": data,
        "services": services,
        "repairs": repairs,
        "critical": critical,
        "warnings": warnings,
    }
    state["last_check_unix"] = result["generated_unix"]
    state["last_status"] = overall
    if repairs:
        state["last_repairs"] = repairs
    atomic_json(STATE_FILE, state)
    atomic_json(STATUS_FILE, result)
    return result


def self_test() -> Dict[str, Any]:
    if CHECK_SECONDS < 60 or CHECK_SECONDS > 3600:
        raise RuntimeError("Guard interval must be between 60 and 3600 seconds")
    result = run_check()
    return {
        "status": "PASSED",
        "guard_status": result["status"],
        "integrity": result["database"]["integrity"],
        "data_checks": len(result["data"]),
        "service_checks": len(result["services"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        print(json.dumps(self_test(), ensure_ascii=False, indent=2), flush=True)
        return
    while True:
        try:
            result = run_check()
            print(
                json.dumps(
                    {
                        "timestamp": result["generated_timestamp"],
                        "status": result["status"],
                        "warnings": result["warnings"],
                        "repairs": result["repairs"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except Exception as exc:
            failure = {
                "version": "RELIABILITY-GUARD-V1",
                "status": "CRITICAL",
                "generated_unix": int(time.time()),
                "generated_timestamp": utc_iso(),
                "critical": [f"{type(exc).__name__}: {exc}"],
            }
            atomic_json(STATUS_FILE, failure)
            print(json.dumps(failure, ensure_ascii=False), flush=True)
            if args.once:
                raise
        if args.once:
            return
        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
