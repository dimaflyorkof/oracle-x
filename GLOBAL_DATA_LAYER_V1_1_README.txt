ORACLE X GLOBAL DATA LAYER V1.1

Adds:
- CFTC Legacy Futures Only positioning for CME Bitcoin contract 133741.
- Official BLS economic-release calendar snapshots.

Safety:
- Historical CFTC rows are available only from first observed_at.
- BLS schedule timestamps are converted from America/New_York to UTC.
- Schedule changes create new snapshots instead of rewriting known history.
- CFTC and calendar remain context-only.
- No source has trading or historical-backtest authority.

Still pending:
- ALFRED requires FRED_API_KEY and a separate vintage backfill.
- CME licensed feeds and consolidated ETF flows need separate contracts.
