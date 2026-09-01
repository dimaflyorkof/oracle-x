from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from core.decision_engine import analyze_decision
from core.regime import analyze_regime
from database.db import connect


@dataclass
class RecordedSignal:
    signal_id: Optional[int]
    symbol: str
    decision: str
    recorded: bool
    reason: str


def utc_now():
    return datetime.now(timezone.utc)


def record_signal(symbol: str = "BTC") -> RecordedSignal:
    decision = analyze_decision(symbol)

    if decision.decision == "NO_TRADE":
        return RecordedSignal(
            signal_id=None,
            symbol=symbol,
            decision=decision.decision,
            recorded=False,
            reason="NO_TRADE не записується як активний торговий сигнал",
        )

    regime = analyze_regime(symbol)

    now = utc_now()
    timestamp = now.isoformat()
    timestamp_unix = int(now.timestamp())

    con = connect()

    try:
        cur = con.cursor()

        cur.execute(
            """
            INSERT INTO signals (
                timestamp,
                timestamp_unix,
                symbol,
                decision,
                oracle_score,
                confidence,
                entry_price,
                stop_loss,
                tp1,
                tp2,
                tp3,
                risk_reward,
                market_regime,
                status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                timestamp,
                timestamp_unix,
                symbol,
                decision.decision,
                decision.score,
                decision.confidence,
                decision.entry,
                decision.stop,
                decision.tp1,
                decision.tp2,
                None,
                decision.rr_tp2,
                regime.regime,
                "OPEN",
            ),
        )

        signal_id = cur.lastrowid

        con.commit()

    finally:
        con.close()

    return RecordedSignal(
        signal_id=signal_id,
        symbol=symbol,
        decision=decision.decision,
        recorded=True,
        reason="Сигнал записано",
    )


if __name__ == "__main__":
    result = record_signal("BTC")

    print()
    print("ORACLE X — OUTCOME TRACKER")
    print("=" * 50)
    print(f"Symbol:    {result.symbol}")
    print(f"Decision:  {result.decision}")
    print(f"Recorded:  {result.recorded}")
    print(f"Signal ID: {result.signal_id}")
    print(f"Reason:    {result.reason}")
