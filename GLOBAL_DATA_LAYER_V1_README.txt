ORACLE X GLOBAL DATA LAYER V1

Purpose
-------
Collect official world-market context without changing V5.1 or authorizing trades.

Enabled sources
---------------
1. U.S. Bureau of Labor Statistics Public Data API:
   CPI, core CPI, PPI final demand, nonfarm payrolls, unemployment.
2. Federal Reserve monetary-policy press-release RSS.

Causal rules
------------
- Every value has available_at and observed_at.
- Historical BLS values first fetched today are available only from today.
- Revisions create a new revision row; history is not overwritten.
- Missing sources remain PENDING, never neutral/zero.
- V1 is CONTEXT ONLY and has no trading authority.

Pending sources
---------------
ALFRED vintages, CFTC COT, CME positioning, BTC ETF flows and the official
release calendar require separate source contracts before activation.

Optional configuration
----------------------
BLS_API_KEY can be placed in /root/oracle-x/.env for expanded BLS limits.
