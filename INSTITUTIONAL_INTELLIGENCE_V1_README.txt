ORACLE X INSTITUTIONAL INTELLIGENCE V1

V1.0.2 uses the official iShares holdings download endpoint with the public
latest-holdings link as fallback. A transient issuer HTTP failure is recorded
as DEGRADED and retried by the timer without disabling the independent CFTC
institutional context layer.

Active inputs:
- CFTC legacy COT, CME Bitcoin contract 133741;
- BlackRock iShares IBIT official daily holdings CSV.

Important semantics:
- IBIT holdings change is a proxy for creations/redemptions, not a reported net-flow number;
- direct daily CME data stays pending until authorized/licensed access exists;
- all availability is recorded at first observation and queried with available_at <= as_of;
- no source has trading or historical-backtest authority;
- missing data is inactive, never zero-filled.

Deployment: shadow/context only. V5.1 and Global Intelligence rules are unchanged.
