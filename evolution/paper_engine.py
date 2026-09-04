from datetime import datetime, timezone
from typing import Dict, List, Optional

from database.db import connect
from evolution.model_registry import get_model


def utc_now():
    now = datetime.now(timezone.utc)
    return now.isoformat(), int(now.timestamp())


def get_paper_test_model(
    symbol: str = "BTC",
) -> Dict:
    con = connect()

    try:
        rows = con.execute(
            """
            SELECT model_version
            FROM model_registry
            WHERE symbol = ?
              AND status = 'PAPER_TEST'
            ORDER BY id DESC
            """,
            (symbol,),
        ).fetchall()

    finally:
        con.close()

    if not rows:
        raise ValueError(
            f"No PAPER_TEST model found for {symbol}"
        )

    if len(rows) > 1:
        versions = ", ".join(
            str(row["model_version"])
            for row in rows
        )

        raise ValueError(
            f"Multiple PAPER_TEST models found for {symbol}: "
            f"{versions}"
        )

    model_version = str(
        rows[0]["model_version"]
    )

    model = get_model(
        symbol=symbol,
        model_version=model_version,
    )

    if model is None:
        raise ValueError(
            f"PAPER_TEST model not found: "
            f"{symbol} {model_version}"
        )

    return model


def get_open_paper_trade(
    symbol: str,
    model_version: str,
) -> Optional[Dict]:
    con = connect()

    try:
        row = con.execute(
            """
            SELECT *
            FROM paper_trades
            WHERE symbol = ?
              AND model_version = ?
              AND status = 'OPEN'
            ORDER BY id DESC
            LIMIT 1
            """,
            (symbol, model_version),
        ).fetchone()

        return dict(row) if row else None

    finally:
        con.close()


def list_open_paper_trades(
    symbol: Optional[str] = None,
) -> List[Dict]:
    con = connect()

    try:
        if symbol is None:
            rows = con.execute(
                """
                SELECT *
                FROM paper_trades
                WHERE status = 'OPEN'
                ORDER BY id DESC
                """
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT *
                FROM paper_trades
                WHERE status = 'OPEN'
                  AND symbol = ?
                ORDER BY id DESC
                """,
                (symbol,),
            ).fetchall()

        return [dict(row) for row in rows]

    finally:
        con.close()


def open_paper_trade(
    symbol: str,
    model_version: str,
    side: str,
    entry_price: float,
    stop_loss: float,
    tp1: float,
    risk_percent: float,
    position_size: float = 0.0,
    regime: Optional[str] = None,
    entry_reason: Optional[str] = None,
    fee_cost: float = 0.0,
    slippage_cost: float = 0.0,
    signal_id: Optional[int] = None,
) -> int:
    side = side.upper()

    if side not in {"LONG", "SHORT"}:
        raise ValueError(
            f"Invalid paper trade side: {side}"
        )

    model = get_model(
        symbol=symbol,
        model_version=model_version,
    )

    if model is None:
        raise ValueError(
            f"Model not found: {symbol} {model_version}"
        )

    if model["status"] != "PAPER_TEST":
        raise ValueError(
            f"Model {model_version} is not in PAPER_TEST "
            f"(current status: {model['status']})"
        )

    existing = get_open_paper_trade(
        symbol=symbol,
        model_version=model_version,
    )

    if existing is not None:
        raise ValueError(
            f"Open paper trade already exists: "
            f"{symbol} v{model_version} "
            f"trade_id={existing['id']}"
        )

    timestamp, timestamp_unix = utc_now()

    con = connect()

    try:
        cur = con.execute(
            """
            INSERT INTO paper_trades (
                signal_id,
                timestamp,
                timestamp_unix,
                symbol,
                model_version,
                side,
                entry_reason,
                regime,
                entry_price,
                stop_loss,
                tp1,
                position_size,
                risk_percent,
                status,
                mfe_r,
                mae_r,
                fee_cost,
                slippage_cost
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?, ?, ?)
            """,
            (
                signal_id,
                timestamp,
                timestamp_unix,
                symbol,
                model_version,
                side,
                entry_reason,
                regime,
                entry_price,
                stop_loss,
                tp1,
                position_size,
                risk_percent,
                0.0,
                0.0,
                fee_cost,
                slippage_cost,
            ),
        )

        con.commit()
        return cur.lastrowid

    finally:
        con.close()


if __name__ == "__main__":
    print("ORACLE X paper engine module loaded")
    print("Open paper trades:", list_open_paper_trades())
