ORACLE X GLOBAL DATA LAYER V1.2.2

Purpose
-------
Add causal ALFRED initial-release macro vintages to the existing official
global context layer without changing V5.1 or authorizing trades.

ALFRED contract
---------------
- The API secret is read only from /root/oracle-x/.env as FRED_API_KEY.
- output_type=4 requests initial-release values rather than latest revisions.
- Both observation and real-time windows begin at 2023-09-01, avoiding the
  FRED vintage-date ceiling for daily series.
- realtime_start records the first date on which each value was known.
- available_at is conservatively set to 00:00 UTC on the following day because
  the ALFRED date alone is not a guaranteed intraday release timestamp.
- Missing values are skipped, never changed to zero.
- Rows whose ALFRED availability precedes their stated observation date are
  rejected rather than shifted; this filters holiday carry-forward anomalies.
- Trading and historical-research authority remain disabled.

Series
------
CPI, core CPI, payrolls, unemployment, effective fed funds, 10Y Treasury,
broad USD index, VIX, high-yield OAS, and Chicago Fed financial conditions.

Operation
---------
The official global collector continues every 15 minutes. ALFRED refreshes
daily through its own oneshot service and persistent timer. An installation
audit verifies all causal fields before the timer is activated.
