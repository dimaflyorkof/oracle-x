import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from database.db import connect


VALID_STATUSES = {
    "CHAMPION",
    "CHALLENGER",
    "PAPER_TEST",
    "REJECTED",
    "RETIRED",
}


def utc_now():
    now = datetime.now(timezone.utc)
    return now.isoformat(), int(now.timestamp())


def register_model(
    symbol: str,
    model_version: str,
    status: str,
    weights: Dict,
    config: Optional[Dict] = None,
    parent_version: Optional[str] = None,
    regime: str = "GLOBAL",
    timeframe: str = "MTF",
    metrics: Optional[Dict] = None,
    reason: Optional[str] = None,
) -> int:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid model status: {status}")

    created_at, created_at_unix = utc_now()

    con = connect()

    try:
        cur = con.execute(
            """
            INSERT INTO model_registry (
                created_at,
                created_at_unix,
                symbol,
                model_version,
                status,
                parent_version,
                regime,
                timeframe,
                weights_json,
                config_json,
                metrics_json,
                reason,
                is_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                created_at_unix,
                symbol,
                model_version,
                status,
                parent_version,
                regime,
                timeframe,
                json.dumps(weights, sort_keys=True),
                json.dumps(config, sort_keys=True) if config else None,
                json.dumps(metrics, sort_keys=True) if metrics else None,
                reason,
                1 if status == "CHAMPION" else 0,
            ),
        )

        con.commit()
        return cur.lastrowid

    finally:
        con.close()


def get_model(
    symbol: str,
    model_version: str,
) -> Optional[Dict]:
    con = connect()

    try:
        row = con.execute(
            """
            SELECT *
            FROM model_registry
            WHERE symbol = ?
              AND model_version = ?
            LIMIT 1
            """,
            (symbol, model_version),
        ).fetchone()

        if row is None:
            return None

        result = dict(row)

        result["weights"] = json.loads(
            result.pop("weights_json")
        )

        config_json = result.pop("config_json")
        result["config"] = (
            json.loads(config_json)
            if config_json
            else {}
        )

        metrics_json = result.pop("metrics_json")

        result["metrics"] = (
            json.loads(metrics_json)
            if metrics_json
            else {}
        )

        return result

    finally:
        con.close()


def get_active_champion(
    symbol: str,
) -> Optional[Dict]:
    con = connect()

    try:
        row = con.execute(
            """
            SELECT *
            FROM model_registry
            WHERE symbol = ?
              AND status = 'CHAMPION'
              AND is_active = 1
            ORDER BY id DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()

        if row is None:
            return None

        result = dict(row)

        result["weights"] = json.loads(
            result.pop("weights_json")
        )

        config_json = result.pop("config_json")
        result["config"] = (
            json.loads(config_json)
            if config_json
            else {}
        )

        metrics_json = result.pop("metrics_json")

        result["metrics"] = (
            json.loads(metrics_json)
            if metrics_json
            else {}
        )

        return result

    finally:
        con.close()


def list_models(
    symbol: str,
    status: Optional[str] = None,
) -> List[Dict]:
    con = connect()

    try:
        if status is None:
            rows = con.execute(
                """
                SELECT *
                FROM model_registry
                WHERE symbol = ?
                ORDER BY id DESC
                """,
                (symbol,),
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT *
                FROM model_registry
                WHERE symbol = ?
                  AND status = ?
                ORDER BY id DESC
                """,
                (symbol, status),
            ).fetchall()

        return [dict(row) for row in rows]

    finally:
        con.close()


def promote_model(
    symbol: str,
    model_version: str,
    reason: str,
) -> None:
    promoted_at, _ = utc_now()

    con = connect()

    try:
        con.execute("BEGIN")

        # Previous champion becomes retired.
        con.execute(
            """
            UPDATE model_registry
            SET status = 'RETIRED',
                is_active = 0
            WHERE symbol = ?
              AND status = 'CHAMPION'
              AND is_active = 1
            """,
            (symbol,),
        )

        cur = con.execute(
            """
            UPDATE model_registry
            SET status = 'CHAMPION',
                is_active = 1,
                promoted_at = ?,
                reason = ?
            WHERE symbol = ?
              AND model_version = ?
            """,
            (
                promoted_at,
                reason,
                symbol,
                model_version,
            ),
        )

        if cur.rowcount != 1:
            raise ValueError(
                f"Model not found: {symbol} {model_version}"
            )

        con.commit()

    except Exception:
        con.rollback()
        raise

    finally:
        con.close()


def reject_model(
    symbol: str,
    model_version: str,
    reason: str,
) -> None:
    rejected_at, _ = utc_now()

    con = connect()

    try:
        cur = con.execute(
            """
            UPDATE model_registry
            SET status = 'REJECTED',
                is_active = 0,
                rejected_at = ?,
                reason = ?
            WHERE symbol = ?
              AND model_version = ?
            """,
            (
                rejected_at,
                reason,
                symbol,
                model_version,
            ),
        )

        if cur.rowcount != 1:
            raise ValueError(
                f"Model not found: {symbol} {model_version}"
            )

        con.commit()

    finally:
        con.close()


def update_model_metrics(
    symbol: str,
    model_version: str,
    metrics_update: Dict,
    reason: Optional[str] = None,
) -> None:
    """
    Merge new validation metrics into existing metrics_json
    without destroying previous model history.
    """
    con = connect()

    try:
        row = con.execute(
            """
            SELECT metrics_json
            FROM model_registry
            WHERE symbol = ?
              AND model_version = ?
            LIMIT 1
            """,
            (symbol, model_version),
        ).fetchone()

        if row is None:
            raise ValueError(
                f"Model not found: {symbol} {model_version}"
            )

        current = (
            json.loads(row["metrics_json"])
            if row["metrics_json"]
            else {}
        )

        current.update(metrics_update)

        if reason is None:
            con.execute(
                """
                UPDATE model_registry
                SET metrics_json = ?
                WHERE symbol = ?
                  AND model_version = ?
                """,
                (
                    json.dumps(current, sort_keys=True),
                    symbol,
                    model_version,
                ),
            )
        else:
            con.execute(
                """
                UPDATE model_registry
                SET metrics_json = ?,
                    reason = ?
                WHERE symbol = ?
                  AND model_version = ?
                """,
                (
                    json.dumps(current, sort_keys=True),
                    reason,
                    symbol,
                    model_version,
                ),
            )

        con.commit()

    finally:
        con.close()
