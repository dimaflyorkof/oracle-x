from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional

from database.db import connect


TIMEFRAME_SECONDS = 15 * 60


def utc_now():
    now = datetime.now(timezone.utc)
    return now.isoformat(), int(now.timestamp())


def get_pending_entry(
    symbol: str,
    model_version: str,
) -> Optional[Dict]:
    con = connect()

    try:
        row = con.execute(
            """
            SELECT *
            FROM paper_pending_entries
            WHERE symbol = ?
              AND model_version = ?
              AND status = 'PENDING'
            ORDER BY id DESC
            LIMIT 1
            """,
            (symbol, model_version),
        ).fetchone()

        return dict(row) if row else None

    finally:
        con.close()


def list_pending_entries(
    symbol: Optional[str] = None,
) -> List[Dict]:
    con = connect()

    try:
        if symbol is None:
            rows = con.execute(
                """
                SELECT *
                FROM paper_pending_entries
                WHERE status = 'PENDING'
                ORDER BY execute_after_unix ASC
                """
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT *
                FROM paper_pending_entries
                WHERE status = 'PENDING'
                  AND symbol = ?
                ORDER BY execute_after_unix ASC
                """,
                (symbol,),
            ).fetchall()

        return [dict(row) for row in rows]

    finally:
        con.close()


def get_model_config(
    symbol: str,
    model_version: str,
) -> Dict:
    con = connect()

    try:
        row = con.execute(
            """
            SELECT status, config_json
            FROM model_registry
            WHERE symbol = ?
              AND model_version = ?
            """,
            (symbol, model_version),
        ).fetchone()

    finally:
        con.close()

    if row is None:
        raise ValueError(
            f"Model not found: {symbol} {model_version}"
        )

    if row["status"] != "PAPER_TEST":
        raise ValueError(
            f"Model {model_version} is not PAPER_TEST "
            f"(status={row['status']})"
        )

    return json.loads(row["config_json"])


def create_pending_entry(
    *,
    symbol: str,
    model_version: str,
    side: str,
    signal_candle_timestamp: str,
    signal_candle_unix: int,
    score: float,
    confidence: float,
    agreement: float,
    signal_atr: float,
    risk_percent: float = 1.0,
    regime: Optional[str] = None,
    entry_reason: Optional[str] = None,
    signal_id: Optional[int] = None,
) -> int:
    side = side.upper()

    if side not in {"LONG", "SHORT"}:
        raise ValueError(
            f"Invalid side: {side}"
        )

    config = get_model_config(
        symbol,
        model_version,
    )

    stop_atr = float(
        config["stop_atr"]
    )

    tp_r = float(
        config["tp_r"]
    )

    min_score = float(
        config["min_score"]
    )

    min_agreement = float(
        config["min_agreement"]
    )

    if abs(score) < min_score:
        raise ValueError(
            f"Score below model threshold: "
            f"{abs(score):.2f} < {min_score:.2f}"
        )

    if agreement < min_agreement:
        raise ValueError(
            f"Agreement below model threshold: "
            f"{agreement:.2f} < {min_agreement:.2f}"
        )

    existing = get_pending_entry(
        symbol,
        model_version,
    )

    if existing is not None:
        raise ValueError(
            f"Pending entry already exists: "
            f"id={existing['id']}"
        )

    created_timestamp, created_unix = utc_now()

    # Candle timestamps are Binance 15m candle OPEN times.
    # A signal becomes actionable at the next candle open,
    # exactly 900 seconds after signal candle open time.
    execute_after_unix = (
        int(signal_candle_unix)
        + TIMEFRAME_SECONDS
    )

    con = connect()

    try:
        cur = con.execute(
            """
            INSERT INTO paper_pending_entries (
                signal_id,
                created_timestamp,
                created_timestamp_unix,
                signal_candle_timestamp,
                signal_candle_unix,
                execute_after_unix,
                symbol,
                model_version,
                side,
                score,
                confidence,
                agreement,
                signal_atr,
                stop_atr,
                tp_r,
                risk_percent,
                regime,
                entry_reason,
                status
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'PENDING'
            )
            """,
            (
                signal_id,
                created_timestamp,
                created_unix,
                signal_candle_timestamp,
                int(signal_candle_unix),
                execute_after_unix,
                symbol,
                model_version,
                side,
                float(score),
                float(confidence),
                float(agreement),
                float(signal_atr),
                stop_atr,
                tp_r,
                float(risk_percent),
                regime,
                entry_reason,
            ),
        )

        con.commit()
        return int(cur.lastrowid)

    finally:
        con.close()


def cancel_pending_entry(
    pending_id: int,
    reason: str,
) -> None:
    con = connect()

    try:
        con.execute(
            """
            UPDATE paper_pending_entries
            SET status = 'CANCELLED',
                cancel_reason = ?
            WHERE id = ?
              AND status = 'PENDING'
            """,
            (
                reason,
                pending_id,
            ),
        )

        con.commit()

    finally:
        con.close()


if __name__ == "__main__":
    print(
        "ORACLE X — PAPER PENDING ENTRY MODULE"
    )
    print(
        "Pending:",
        list_pending_entries(),
    )
