ORACLE X DERIVATIVES AVAILABILITY CONTRACT

Files:
- database/db.py
- collectors/derivatives/binance_futures.py
- core/market_intelligence.py
- evolution/derivatives_contract_audit.py
- evolution/flow_edge_search_v3.py

Contract:
- timestamp_unix: retained legacy storage/bucket timestamp.
- event_timestamp_unix: start/event time represented by the observation.
- available_at_unix: earliest conservative time the observation may be used.
- data_interval_seconds: represented interval length.
- data_kind: EVENT, HISTORICAL_HOURLY_AGGREGATE, or LIVE_5M_COMPOSITE.

Causality policy:
- Historical hourly aggregates are available at event time + 3600 seconds.
- Live 5m composites are available no earlier than event time + 300 seconds.
- Market Intelligence filters by available_at_unix.
- Derivatives are compared as completed UTC hours, preventing 5m live rows
  from receiving twelve times the weight of historical hourly rows.
- Repeated funding values are deduplicated before scoring.
- Funding-only rows are point-in-time EVENT records, including OKX backfills.
- Each exchange is excluded independently after six hours without fresh data.

Validation completed before packaging:
- Python syntax compile: passed for all files.
- Legacy schema migration in a temporary database: passed twice (idempotent).
- Historical hourly visibility before hour close: blocked.
- Live collector INSERT contract: passed.

Do not promote a model directly from flow_edge_search_v3. Its final historical
period has already informed development. A passing result authorizes only new
forward paper/shadow validation.
