from __future__ import annotations

from typing import Dict, Optional

from core.decision_engine import analyze_decision
from database.db import connect
from evolution.paper_engine import (
    get_paper_test_model,
    get_open_paper_trade,
)
from evolution.paper_pending import (
    create_pending_entry,
    get_pending_entry,
)


def latest_closed_15m(
    symbol: str,
) -> Optional[Dict]:
    con = connect()

    try:
        row = con.execute(
            """
            SELECT
                timestamp,
                timestamp_unix,
                close
            FROM market_snapshots
            WHERE symbol = ?
              AND timeframe = '15m'
              AND close IS NOT NULL
            ORDER BY timestamp_unix DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()

        return dict(row) if row else None

    finally:
        con.close()


def run_paper_signal(
    symbol: str = "BTC",
) -> Dict:
    model = get_paper_test_model(symbol)

    model_version = str(
        model["model_version"]
    )

    open_trade = get_open_paper_trade(
        symbol,
        model_version,
    )

    if open_trade is not None:
        return {
            "action": "SKIP",
            "reason": "OPEN_PAPER_TRADE_EXISTS",
            "trade_id": open_trade["id"],
            "model_version": model_version,
        }

    pending = get_pending_entry(
        symbol,
        model_version,
    )

    if pending is not None:
        return {
            "action": "SKIP",
            "reason": "PENDING_ENTRY_EXISTS",
            "pending_id": pending["id"],
            "execute_after_unix": pending["execute_after_unix"],
            "model_version": model_version,
        }

    candle = latest_closed_15m(symbol)

    if candle is None:
        return {
            "action": "SKIP",
            "reason": "NO_CLOSED_15M_CANDLE",
            "model_version": model_version,
        }

    decision = analyze_decision(symbol)

    if decision.decision not in {
        "LONG",
        "SHORT",
    }:
        return {
            "action": "NO_TRADE",
            "decision": decision.decision,
            "score": decision.score,
            "confidence": decision.confidence,
            "model_version": model_version,
        }

    scoring_data = (
        decision.data.get("scoring", {})
        if isinstance(decision.data, dict)
        else {}
    )

    agreement = scoring_data.get(
        "agreement"
    )

    if agreement is None:
        raise RuntimeError(
            "Decision Engine did not provide scoring agreement"
        )

    risk_data = (
        decision.data.get("risk", {})
        if isinstance(decision.data, dict)
        else {}
    )

    risk_entry = risk_data.get("entry")
    risk_stop = risk_data.get("stop")

    if risk_entry is None or risk_stop is None:
        raise RuntimeError(
            "Decision Engine did not provide risk entry/stop"
        )

    model_config = model.get("config", {})
    stop_atr = float(
        model_config.get("stop_atr", 1.7)
    )

    if stop_atr <= 0:
        raise RuntimeError(
            "Invalid stop_atr in model config"
        )

    signal_atr = (
        abs(float(risk_entry) - float(risk_stop))
        / stop_atr
    )

    entry_reason = (
        f"Decision={decision.decision}; "
        f"score={decision.score}; "
        f"confidence={decision.confidence}; "
        f"model={model_version}"
    )

    pending_id = create_pending_entry(
        symbol=symbol,
        model_version=model_version,
        side=decision.decision,
        signal_candle_timestamp=str(
            candle["timestamp"]
        ),
        signal_candle_unix=int(
            candle["timestamp_unix"]
        ),
        score=float(decision.score),
        confidence=float(
            decision.confidence
        ),
        agreement=float(agreement),
        signal_atr=float(signal_atr),
        risk_percent=1.0,
        regime=None,
        entry_reason=entry_reason,
        signal_id=None,
    )

    return {
        "action": "PENDING_CREATED",
        "pending_id": pending_id,
        "symbol": symbol,
        "model_version": model_version,
        "side": decision.decision,
        "signal_candle": candle["timestamp"],
        "execute_after_unix": (
            int(candle["timestamp_unix"])
            + 900
        ),
        "score": decision.score,
        "confidence": decision.confidence,
        "agreement": agreement,
        "signal_atr": round(signal_atr, 6),
    }


if __name__ == "__main__":
    result = run_paper_signal("BTC")

    print()
    print("ORACLE X — PAPER SIGNAL RUNNER")
    print("=" * 60)

    for key, value in result.items():
        print(f"{key}: {value}")
