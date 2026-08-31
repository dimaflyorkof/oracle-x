import json
import sqlite3
from datetime import datetime, timezone

from config.settings import DB_PATH


SYMBOL = "BTC"


FRESHNESS_LIMITS = {
    "market": 2 * 3600,
    "orderflow": 5 * 60,
    "sentiment": 36 * 3600,
    "onchain": 48 * 3600,
    "macro": 72 * 3600,
}

DERIVATIVES_LIMIT = 6 * 3600


def now_utc():
    return datetime.now(timezone.utc)


def one(con, sql, params=()):
    return con.execute(
        sql,
        params,
    ).fetchone()


def latest_market(con):
    row = one(
        con,
        """
        SELECT
            timestamp,
            timestamp_unix,
            price,
            open,
            high,
            low,
            close,
            volume,
            timeframe,
            source
        FROM market_snapshots
        WHERE symbol = ?
          AND timeframe = '1h'
        ORDER BY timestamp_unix DESC
        LIMIT 1
        """,
        (SYMBOL,),
    )

    if not row:
        return None

    return {
        "timestamp": row[0],
        "timestamp_unix": row[1],
        "price": row[2] or row[6],
        "open": row[3],
        "high": row[4],
        "low": row[5],
        "close": row[6],
        "volume": row[7],
        "timeframe": row[8],
        "source": row[9],
    }


def latest_derivatives(con):
    sources = [
        row[0]
        for row in con.execute(
            """
            SELECT DISTINCT source
            FROM derivatives_history
            WHERE symbol = ?
            """,
            (SYMBOL,),
        )
    ]

    result = {}

    for source in sources:
        row = one(
            con,
            """
            SELECT
                timestamp,
                timestamp_unix,
                funding_rate,
                open_interest,
                open_interest_change,
                long_ratio,
                short_ratio,
                long_short_ratio,
                taker_buy_volume,
                taker_sell_volume,
                taker_ratio,
                futures_basis
            FROM derivatives_history
            WHERE symbol = ?
              AND source = ?
            ORDER BY timestamp_unix DESC
            LIMIT 1
            """,
            (
                SYMBOL,
                source,
            ),
        )

        if not row:
            continue

        result[source] = {
            "timestamp": row[0],
            "timestamp_unix": row[1],
            "funding_rate": row[2],
            "open_interest": row[3],
            "open_interest_change": row[4],
            "long_ratio": row[5],
            "short_ratio": row[6],
            "long_short_ratio": row[7],
            "taker_buy_volume": row[8],
            "taker_sell_volume": row[9],
            "taker_ratio": row[10],
            "futures_basis": row[11],
        }

    return result


def latest_orderflow(con):
    row = one(
        con,
        """
        SELECT
            timestamp,
            timestamp_unix,
            buy_volume,
            sell_volume,
            delta,
            cvd,
            imbalance
        FROM orderflow_history
        WHERE symbol = ?
        ORDER BY timestamp_unix DESC
        LIMIT 1
        """,
        (SYMBOL,),
    )

    if not row:
        return None

    return {
        "timestamp": row[0],
        "timestamp_unix": row[1],
        "buy_volume": row[2],
        "sell_volume": row[3],
        "delta": row[4],
        "cvd": row[5],
        "imbalance": row[6],
    }


def recent_liquidations(con, now_ts):
    since = now_ts - 3600

    row = one(
        con,
        """
        SELECT
            COUNT(*),
            COALESCE(SUM(long_liquidations), 0),
            COALESCE(SUM(short_liquidations), 0),
            COALESCE(SUM(total_liquidations), 0)
        FROM liquidation_history
        WHERE symbol = ?
          AND timestamp_unix >= ?
        """,
        (
            SYMBOL,
            since,
        ),
    )

    return {
        "window": "1h",
        "window_timestamp_unix": now_ts,
        "events": row[0],
        "long_liquidations": row[1],
        "short_liquidations": row[2],
        "total_liquidations": row[3],
    }


def latest_sentiment(con):
    row = one(
        con,
        """
        SELECT
            timestamp,
            timestamp_unix,
            sentiment_score,
            headline,
            source
        FROM sentiment_history
        WHERE symbol = ?
        ORDER BY timestamp_unix DESC
        LIMIT 1
        """,
        (SYMBOL,),
    )

    if not row:
        return None

    return {
        "timestamp": row[0],
        "timestamp_unix": row[1],
        "score": row[2],
        "classification": row[3],
        "source": row[4],
    }


def latest_onchain(con):
    row = one(
        con,
        """
        SELECT
            timestamp,
            timestamp_unix,
            hash_rate,
            tx_count,
            tx_volume_usd,
            miners_revenue_usd,
            source
        FROM onchain_history
        WHERE symbol = ?
        ORDER BY timestamp_unix DESC
        LIMIT 1
        """,
        (SYMBOL,),
    )

    if not row:
        return None

    return {
        "timestamp": row[0],
        "timestamp_unix": row[1],
        "hash_rate": row[2],
        "tx_count": row[3],
        "tx_volume_usd": row[4],
        "miners_revenue_usd": row[5],
        "source": row[6],
    }


def latest_macro(con):
    row = one(
        con,
        """
        SELECT
            timestamp,
            timestamp_unix,
            dxy,
            nasdaq,
            sp500,
            vix,
            us10y,
            gold
        FROM macro_history
        ORDER BY timestamp_unix DESC
        LIMIT 1
        """,
    )

    if not row:
        return None

    return {
        "timestamp": row[0],
        "timestamp_unix": row[1],
        "dxy": row[2],
        "nasdaq": row[3],
        "sp500": row[4],
        "vix": row[5],
        "us10y": row[6],
        "gold": row[7],
    }


def age_seconds(now_ts, timestamp_unix):
    if timestamp_unix is None:
        return None

    return max(
        0,
        now_ts - int(timestamp_unix),
    )


def freshness_score(age, limit):
    if age is None:
        return 0.0

    if age <= limit:
        return 100.0

    if age <= limit * 2:
        return 70.0

    if age <= limit * 4:
        return 40.0

    return 0.0


def build_freshness(features, now_ts):
    result = {}

    for layer in [
        "market",
        "orderflow",
        "sentiment",
        "onchain",
        "macro",
    ]:
        data = features.get(layer)

        if not data:
            result[layer] = {
                "age_seconds": None,
                "score": 0.0,
                "status": "MISSING",
            }
            continue

        age = age_seconds(
            now_ts,
            data.get("timestamp_unix"),
        )

        limit = FRESHNESS_LIMITS[layer]

        score = freshness_score(
            age,
            limit,
        )

        status = (
            "FRESH"
            if score == 100
            else "STALE"
        )

        result[layer] = {
            "age_seconds": age,
            "max_age_seconds": limit,
            "score": score,
            "status": status,
        }

    derivatives = {}

    for source, data in (
        features.get("derivatives") or {}
    ).items():
        age = age_seconds(
            now_ts,
            data.get("timestamp_unix"),
        )

        score = freshness_score(
            age,
            DERIVATIVES_LIMIT,
        )

        derivatives[source] = {
            "age_seconds": age,
            "max_age_seconds": DERIVATIVES_LIMIT,
            "score": score,
            "status": (
                "FRESH"
                if score == 100
                else "STALE"
            ),
        }

    result["derivatives"] = derivatives

    result["liquidations"] = {
        "age_seconds": 0,
        "score": 100.0,
        "status": "CURRENT_WINDOW",
    }

    return result


def completeness(features):
    layers = [
        "market",
        "derivatives",
        "orderflow",
        "liquidations",
        "sentiment",
        "onchain",
        "macro",
    ]

    available = 0

    for layer in layers:
        if features.get(layer):
            available += 1

    return round(
        available / len(layers) * 100,
        2,
    )


def quality_score(freshness):
    scores = []

    for layer in [
        "market",
        "orderflow",
        "liquidations",
        "sentiment",
        "onchain",
        "macro",
    ]:
        scores.append(
            freshness[layer]["score"]
        )

    derivative_scores = [
        item["score"]
        for item in freshness[
            "derivatives"
        ].values()
    ]

    if derivative_scores:
        scores.append(
            sum(derivative_scores)
            / len(derivative_scores)
        )
    else:
        scores.append(0.0)

    return round(
        sum(scores) / len(scores),
        2,
    )


def build_snapshot():
    con = sqlite3.connect(DB_PATH)

    now = now_utc()
    now_ts = int(now.timestamp())

    features = {
        "timestamp": now.isoformat(),
        "timestamp_unix": now_ts,
        "symbol": SYMBOL,
        "market": latest_market(con),
        "derivatives": latest_derivatives(con),
        "orderflow": latest_orderflow(con),
        "liquidations": recent_liquidations(
            con,
            now_ts,
        ),
        "sentiment": latest_sentiment(con),
        "onchain": latest_onchain(con),
        "macro": latest_macro(con),
    }

    completeness_score = completeness(
        features
    )

    freshness = build_freshness(
        features,
        now_ts,
    )

    source_quality = quality_score(
        freshness
    )

    features[
        "data_completeness_percent"
    ] = completeness_score

    features[
        "freshness"
    ] = freshness

    features[
        "source_quality_score"
    ] = source_quality

    market = features.get(
        "market"
    ) or {}

    price = market.get(
        "price"
    )

    con.execute(
        """
        INSERT INTO oracle_snapshots (
            timestamp,
            timestamp_unix,
            symbol,
            price,
            market_regime,
            source_quality_score,
            raw_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            now.isoformat(),
            now_ts,
            SYMBOL,
            price,
            "UNCLASSIFIED",
            source_quality,
            json.dumps(
                features,
                ensure_ascii=False,
            ),
        ),
    )

    con.commit()

    snapshot_id = con.execute(
        "SELECT last_insert_rowid()"
    ).fetchone()[0]

    con.close()

    return snapshot_id, features


def main():
    print()
    print(
        "ORACLE X - UNIFIED FEATURE SNAPSHOT"
    )
    print()

    snapshot_id, features = (
        build_snapshot()
    )

    print(
        json.dumps(
            features,
            indent=2,
            ensure_ascii=False,
        )
    )

    print()
    print(
        "Snapshot ID:",
        snapshot_id,
    )

    print(
        "Completeness:",
        features[
            "data_completeness_percent"
        ],
        "%",
    )

    print(
        "Source quality:",
        features[
            "source_quality_score"
        ],
        "%",
    )

    print()
    print(
        "FEATURE SNAPSHOT COMPLETE"
    )


if __name__ == "__main__":
    main()
