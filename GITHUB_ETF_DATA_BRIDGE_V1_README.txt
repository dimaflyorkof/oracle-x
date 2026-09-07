ORACLE X GITHUB ETF DATA BRIDGE V1

Purpose
- Fetch public BTC ETF data outside the trading server when data providers block
  data-centre IP addresses.
- Preserve source URL, trust tier, missing values and a canonical SHA256 digest.

Sources
- BlackRock iShares IBIT holdings: issuer-primary.
- Farside Investors BTC ETF flows: regulated secondary aggregator.

Safety
- Data bridge only.
- Trading authority: false.
- Historical-backtest authority: false.
- Missing values remain null and are never converted to zero.
- Recent Farside records are marked RECENT_MAY_REVISE.
- The workflow changes the dataset only when validated source data changes.

Schedule
- 04:35 UTC Tuesday through Saturday, after the US trading session.
- Manual workflow dispatch is also available.
