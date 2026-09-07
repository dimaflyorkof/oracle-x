from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict


DB_PATH = Path(
    os.getenv("ORACLE_X_DB", "/root/oracle-x/database/oracle_x.db")
)
BACKUP_DIR = Path(
    os.getenv("ORACLE_X_BACKUP_DIR", "/root/oracle-x/backups/database")
)
STATUS_FILE = Path(
    os.getenv(
        "ORACLE_X_BACKUP_STATUS",
        "/root/oracle-x/runtime/backup_status.json",
    )
)
KEEP_BACKUPS = int(os.getenv("ORACLE_X_BACKUP_KEEP", "14"))


def utc_iso(timestamp_unix: int | None = None) -> str:
    value = int(timestamp_unix if timestamp_unix is not None else time.time())
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_paths() -> None:
    if not DB_PATH.is_absolute() or not DB_PATH.is_file():
        raise RuntimeError(f"Database not found: {DB_PATH}")
    if KEEP_BACKUPS < 2 or KEEP_BACKUPS > 90:
        raise RuntimeError("ORACLE_X_BACKUP_KEEP must be between 2 and 90")


def source_quick_check() -> str:
    uri = f"file:{DB_PATH}?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=30)
    try:
        row = con.execute("PRAGMA quick_check").fetchone()
        return str(row[0]) if row else "NO_RESULT"
    finally:
        con.close()


def prune_backups() -> int:
    files = sorted(
        BACKUP_DIR.glob("oracle_x_*.db.gz"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for path in files[KEEP_BACKUPS:]:
        if path.parent.resolve() != BACKUP_DIR.resolve():
            raise RuntimeError("Refusing to prune outside backup directory")
        path.unlink()
        removed += 1
    return removed


def run_backup() -> Dict[str, Any]:
    validate_paths()
    started_unix = int(time.time())
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        previous = {}
    last_success_unix = int(
        previous.get("last_success_unix")
        or (previous.get("generated_unix") if previous.get("status") == "OK" else 0)
        or 0
    )
    atomic_json(
        STATUS_FILE,
        {
            "status": "RUNNING",
            "started_unix": started_unix,
            "started_timestamp": utc_iso(started_unix),
            "last_success_unix": last_success_unix or None,
        },
    )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    raw_temporary = BACKUP_DIR / f"oracle_x_{stamp}.db.partial"
    gzip_temporary = BACKUP_DIR / f"oracle_x_{stamp}.db.gz.partial"
    final_path = BACKUP_DIR / f"oracle_x_{stamp}.db.gz"

    try:
        source = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro", uri=True, timeout=60
        )
        destination = sqlite3.connect(str(raw_temporary), timeout=60)
        try:
            source.backup(destination, pages=4096, sleep=0.05)
            row = destination.execute("PRAGMA quick_check").fetchone()
            integrity = str(row[0]) if row else "NO_RESULT"
            if integrity.lower() != "ok":
                raise RuntimeError(f"Backup integrity check failed: {integrity}")
        finally:
            destination.close()
            source.close()

        with raw_temporary.open("rb") as source_file:
            with gzip.open(gzip_temporary, "wb", compresslevel=6) as target:
                for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
                    target.write(chunk)
        os.replace(gzip_temporary, final_path)
        raw_temporary.unlink(missing_ok=True)

        completed_unix = int(time.time())
        result = {
            "status": "OK",
            "started_unix": started_unix,
            "generated_unix": completed_unix,
            "generated_timestamp": utc_iso(completed_unix),
            "last_success_unix": completed_unix,
            "duration_seconds": completed_unix - started_unix,
            "file": str(final_path),
            "compressed_bytes": final_path.stat().st_size,
            "sha256": sha256_file(final_path),
            "integrity": integrity,
            "retained": KEEP_BACKUPS,
            "pruned": prune_backups(),
        }
        atomic_json(STATUS_FILE, result)
        return result
    except Exception as exc:
        raw_temporary.unlink(missing_ok=True)
        gzip_temporary.unlink(missing_ok=True)
        failure = {
            "status": "FAILED",
            "started_unix": started_unix,
            "generated_unix": int(time.time()),
            "generated_timestamp": utc_iso(),
            "last_success_unix": last_success_unix or None,
            "error": f"{type(exc).__name__}: {exc}",
        }
        atomic_json(STATUS_FILE, failure)
        raise


def self_test() -> Dict[str, Any]:
    validate_paths()
    integrity = source_quick_check()
    if integrity.lower() != "ok":
        raise RuntimeError(f"Source database quick_check failed: {integrity}")
    return {
        "status": "PASSED",
        "database": str(DB_PATH),
        "integrity": integrity,
        "backup_directory": str(BACKUP_DIR),
        "retention": KEEP_BACKUPS,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    result = self_test() if args.self_test else run_backup()
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
