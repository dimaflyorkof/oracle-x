import sqlite3
import json
from datetime import datetime, timezone

from config.settings import DB_PATH


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row

    connection.execute("PRAGMA journal_mode=WAL;")
    connection.execute("PRAGMA foreign_keys=ON;")

    return connection


def init_database():
    con = connect()
    cur = con.cursor()

    # =====================================================
    # MARKET SNAPSHOTS
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,

            price REAL,

            open REAL,
            high REAL,
            low REAL,
            close REAL,

            volume REAL,

            source TEXT,
            source_quality REAL,

            raw_json TEXT
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_market_snapshot
        ON market_snapshots (
            symbol,
            timeframe,
            timestamp_unix
        )
    """)
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_market_snapshot_unique
        ON market_snapshots (
            symbol,   
            timeframe,
            source,
            timestamp_unix
        )
    """)
    # =====================================================
    # DERIVATIVES HISTORY
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS derivatives_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,
            source TEXT NOT NULL,

            funding_rate REAL,

            open_interest REAL,
            open_interest_change REAL,

            long_ratio REAL,
            short_ratio REAL,
            long_short_ratio REAL,

            taker_buy_volume REAL,
            taker_sell_volume REAL,
            taker_ratio REAL,

            futures_basis REAL,

            raw_json TEXT
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_derivatives
        ON derivatives_history (
            symbol,
            source,
            timestamp_unix
        )
    """)

    # =====================================================
    # ORDER FLOW HISTORY
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orderflow_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,
            source TEXT NOT NULL,

            bid_volume REAL,
            ask_volume REAL,

            imbalance REAL,

            buy_volume REAL,
            sell_volume REAL,

            delta REAL,
            cvd REAL,

            spread REAL,

            raw_json TEXT
        )
    """)

    # =====================================================
    # LIQUIDATION HISTORY
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS liquidation_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,
            source TEXT NOT NULL,

            long_liquidations REAL,
            short_liquidations REAL,

            total_liquidations REAL,

            dominant_side TEXT,

            raw_json TEXT
        )
    """)

    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_liquidation_unique
        ON liquidation_history (
            timestamp_unix,
            symbol,
            source,
            dominant_side,
            total_liquidations
        )
    """)

    # =====================================================
    # ON-CHAIN / WHALE HISTORY
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS onchain_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,
            source TEXT NOT NULL,

            exchange_inflow REAL,
            exchange_outflow REAL,
            exchange_netflow REAL,

            whale_inflow REAL,
            whale_outflow REAL,

            stablecoin_flow REAL,

            miner_flow REAL,

            raw_json TEXT
        )
    """)

    # =====================================================
    # ETF / INSTITUTIONAL
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS institutional_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT,

            source TEXT NOT NULL,

            etf_netflow REAL,

            cme_open_interest REAL,
            cme_basis REAL,

            institutional_score REAL,

            raw_json TEXT
        )
    """)

    # =====================================================
    # MACRO HISTORY
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS macro_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            dxy REAL,
            nasdaq REAL,
            sp500 REAL,
            vix REAL,

            us10y REAL,
            gold REAL,

            btc_dominance REAL,
            total_market_cap REAL,

            fear_greed REAL,

            raw_json TEXT
        )
    """)

    # =====================================================
    # NEWS / SENTIMENT
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS sentiment_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT,

            source TEXT,

            sentiment_score REAL,
            news_impact_score REAL,

            headline TEXT,

            raw_json TEXT
        )
    """)

    # =====================================================
    # COMPLETE ORACLE SNAPSHOT
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS oracle_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,

            price REAL,

            market_regime TEXT,

            trend_score REAL,
            spot_score REAL,
            derivatives_score REAL,
            orderflow_score REAL,
            liquidation_score REAL,
            options_score REAL,
            onchain_score REAL,
            whale_score REAL,
            etf_score REAL,
            macro_score REAL,
            sentiment_score REAL,
            historical_twin_score REAL,

            source_quality_score REAL,

            oracle_score REAL,

            long_probability REAL,
            short_probability REAL,
            no_trade_probability REAL,

            decision TEXT,
            confidence REAL,

            raw_json TEXT
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_oracle_snapshot
        ON oracle_snapshots (
            symbol,
            timestamp_unix
        )
    """)

    # =====================================================
    # SIGNAL HISTORY / PROOF OF PERFORMANCE
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,

            decision TEXT NOT NULL,
            oracle_score REAL,
            confidence REAL,

            entry_price REAL,
            stop_loss REAL,

            tp1 REAL,
            tp2 REAL,
            tp3 REAL,

            risk_reward REAL,

            market_regime TEXT,

            regime_component REAL,
            structure_component REAL,
            momentum_component REAL,
            orderflow_component REAL,
            derivatives_component REAL,
            liquidations_component REAL,

            snapshot_id INTEGER,

            status TEXT DEFAULT 'OPEN',

            result TEXT,

            result_r REAL,

            closed_timestamp TEXT,

            FOREIGN KEY(snapshot_id)
            REFERENCES oracle_snapshots(id)
        )
    """)

    # =====================================================
    # PAPER TRADING
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS paper_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            signal_id INTEGER,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,

            side TEXT NOT NULL,

            entry_price REAL,
            stop_loss REAL,

            tp1 REAL,
            tp2 REAL,
            tp3 REAL,

            position_size REAL,

            risk_percent REAL,

            status TEXT DEFAULT 'OPEN',

            exit_price REAL,
            pnl REAL,
            pnl_percent REAL,
            result_r REAL,

            closed_timestamp TEXT,

            FOREIGN KEY(signal_id)
            REFERENCES signals(id)
        )
    """)

    # =====================================================
    # HISTORICAL TWINS
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS historical_twins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,

            current_snapshot_id INTEGER,

            matched_snapshot_id INTEGER,

            similarity_score REAL,

            market_regime TEXT,

            outcome_1h REAL,
            outcome_4h REAL,
            outcome_24h REAL,

            max_favorable_excursion REAL,
            max_adverse_excursion REAL,

            FOREIGN KEY(current_snapshot_id)
            REFERENCES oracle_snapshots(id),

            FOREIGN KEY(matched_snapshot_id)
            REFERENCES oracle_snapshots(id)
        )
    """)

    # =====================================================
    # SOURCE QUALITY
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS source_quality (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            source TEXT NOT NULL,

            category TEXT NOT NULL,

            latency_ms REAL,
            uptime_score REAL,

            liquidity_score REAL,
            spread_score REAL,

            anomaly_score REAL,

            historical_accuracy REAL,

            final_weight REAL
        )
    """)

    # =====================================================
    # ADAPTIVE FEATURE WEIGHTS
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feature_weights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,
            timestamp_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,
            regime TEXT NOT NULL,
            timeframe TEXT NOT NULL,

            feature_name TEXT NOT NULL,

            weight REAL NOT NULL,
            sample_size INTEGER DEFAULT 0,

            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,

            win_rate REAL,
            average_r REAL,

            model_version TEXT DEFAULT '1.0'
        )
    """)

    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_feature_weights_unique
        ON feature_weights (
            symbol,
            regime,
            timeframe,
            feature_name,
            model_version
        )
    """)

    # =====================================================
    # MODEL REGISTRY
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS model_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            created_at TEXT NOT NULL,
            created_at_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,
            model_version TEXT NOT NULL,

            status TEXT NOT NULL,

            parent_version TEXT,

            regime TEXT DEFAULT 'GLOBAL',
            timeframe TEXT DEFAULT 'MTF',

            weights_json TEXT NOT NULL,
            metrics_json TEXT,

            reason TEXT,

            promoted_at TEXT,
            rejected_at TEXT,

            is_active INTEGER DEFAULT 0
        )
    """)

    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_model_registry_unique
        ON model_registry (
            symbol,
            model_version
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_model_registry_status
        ON model_registry (
            symbol,
            status,
            is_active
        )
    """)

    # =====================================================
    # EXPERIMENT REGISTRY
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS experiment_registry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            created_at TEXT NOT NULL,
            created_at_unix INTEGER NOT NULL,

            symbol TEXT NOT NULL,

            experiment_name TEXT NOT NULL,

            champion_version TEXT NOT NULL,
            challenger_version TEXT NOT NULL,

            status TEXT NOT NULL,

            hypothesis TEXT,

            champion_metrics_json TEXT,
            challenger_metrics_json TEXT,

            improvement_percent REAL,

            decision TEXT,
            decision_reason TEXT,

            started_at TEXT,
            completed_at TEXT
        )
    """)

    cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_experiment_registry
        ON experiment_registry (
            symbol,
            status,
            created_at_unix
        )
    """)

    # =====================================================
    # PERFORMANCE
    # =====================================================

    cur.execute("""
        CREATE TABLE IF NOT EXISTS performance_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            timestamp TEXT NOT NULL,

            symbol TEXT,

            period TEXT,

            total_signals INTEGER,

            wins INTEGER,
            losses INTEGER,
            breakeven INTEGER,

            win_rate REAL,

            profit_factor REAL,

            total_r REAL,

            average_r REAL,

            max_drawdown REAL,

            average_confidence REAL,

            raw_json TEXT
        )
    """)

    con.commit()
    con.close()


def insert_json(table, data):
    """
    Generic helper for inserting dictionaries into
    approved ORACLE tables.
    """

    allowed_tables = {
        "market_snapshots",
        "derivatives_history",
        "orderflow_history",
        "liquidation_history",
        "onchain_history",
        "institutional_history",
        "macro_history",
        "sentiment_history",
        "oracle_snapshots",
        "signals",
        "paper_trades",
        "historical_twins",
        "source_quality",
        "performance_metrics",
    }

    if table not in allowed_tables:
        raise ValueError("Table is not allowed")

    prepared = dict(data)

    if "raw_json" in prepared:
        value = prepared["raw_json"]

        if not isinstance(value, str):
            prepared["raw_json"] = json.dumps(
                value,
                ensure_ascii=False
            )

    columns = ", ".join(prepared.keys())

    placeholders = ", ".join(
        ["?"] * len(prepared)
    )

    values = list(prepared.values())

    insert_mode = (
    "INSERT OR IGNORE"
    if table == "market_snapshots"
    else "INSERT"
    )

    sql = (
    f"{insert_mode} INTO {table} "
    f"({columns}) "
    f"VALUES ({placeholders})"
    )

    con = connect()
    cur = con.cursor()

    cur.execute(sql, values)

    row_id = cur.lastrowid

    con.commit()
    con.close()

    return row_id


if __name__ == "__main__":
    init_database()

    print("ORACLE X database initialized")
    print(f"Database: {DB_PATH}")
