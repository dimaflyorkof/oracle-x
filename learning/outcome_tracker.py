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


def evaluate_open_signal(signal_id: int) -> Optional[str]:
    con = connect()

    try:
        signal = con.execute(
            """
            SELECT *
            FROM signals
            WHERE id = ?
              AND status = 'OPEN'
            """,
            (signal_id,),
        ).fetchone()

        if signal is None:
            return None

        direction = signal["decision"]
        entry = signal["entry_price"]
        stop = signal["stop_loss"]
        tp1 = signal["tp1"]
        tp2 = signal["tp2"]

        candles = con.execute(
            """
            SELECT timestamp, timestamp_unix, high, low, close
            FROM market_snapshots
            WHERE symbol = ?
              AND timeframe = '5m'
              AND timestamp_unix > ?
            ORDER BY timestamp_unix ASC
            """,
            (
                signal["symbol"],
                signal["timestamp_unix"],
            ),
        ).fetchall()

        if not candles:
            return "NO_FUTURE_DATA"

        result = None
        result_r = None
        closed_timestamp = None

        for candle in candles:
            high = float(candle["high"])
            low = float(candle["low"])

            if direction == "LONG":
                stop_hit = stop is not None and low <= stop
                tp1_hit = tp1 is not None and high >= tp1
                tp2_hit = tp2 is not None and high >= tp2

            elif direction == "SHORT":
                stop_hit = stop is not None and high >= stop
                tp1_hit = tp1 is not None and low <= tp1
                tp2_hit = tp2 is not None and low <= tp2

            else:
                return "INVALID_DIRECTION"

            # Conservative rule:
            # if stop and target are touched in the same candle,
            # count stop first because intrabar order is unknown.
            if stop_hit:
                result = "STOP"
                result_r = -1.0
                closed_timestamp = candle["timestamp"]
                break

            if tp2_hit:
                result = "TP2"
                result_r = signal["risk_reward"]
                closed_timestamp = candle["timestamp"]
                break

            if tp1_hit:
                result = "TP1"

                if entry is not None and stop is not None and tp1 is not None:
                    risk = abs(entry - stop)

                    if risk > 0:
                        reward = abs(tp1 - entry)
                        result_r = reward / risk

                closed_timestamp = candle["timestamp"]
                break

        if result is None:
            return "STILL_OPEN"

        con.execute(
            """
            UPDATE signals
            SET status = 'CLOSED',
                result = ?,
                result_r = ?,
                closed_timestamp = ?
            WHERE id = ?
            """,
            (
                result,
                result_r,
                closed_timestamp,
                signal_id,
            ),
        )

        con.commit()

        return result

    finally:
        con.close()
