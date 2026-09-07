ORACLE X GLOBAL INTELLIGENCE V2.1

Purpose:
- add official ALFRED initial-release macro context to Global Intelligence V2;
- preserve event-time visibility with available_at_unix <= decision time;
- collect independent forward A/B evidence without changing V5.1 rules.

Safety:
- SHADOW ONLY;
- no trading authority;
- no historical backtest authority;
- missing or stale data is inactive, never zero-filled;
- fixed series directions and weights are not fitted to V5.1 outcomes.

The existing V5.1, Global V1 and Global V2 services remain unchanged.
