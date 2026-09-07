from datetime import date

from operations.github_etf_data_bridge import parse_blackrock, parse_farside


BLACKROCK = b'''iShares Bitcoin Trust ETF
Fund Holdings as of,"Sep 04, 2026"
Shares Outstanding,"1,384,960,000.00"

Ticker,Name,Sector,Asset Class,Market Value,Weight (%),Notional Value,Quantity,Market Currency,Accrual Date
"BTC","BITCOIN","-","Alternative","62,415,881,956.79","100.00","62,415,881,956.79","785,635.31730","BTC","-"
'''

FARSIDE = b'''<html><table><tr><th>Date</th><th>IBIT</th><th>FBTC</th><th>GBTC</th><th>Total</th></tr>
<tr><td>03 Sep 2026</td><td>100.5</td><td>-</td><td>(20.0)</td><td>80.5</td></tr>
<tr><td>04 Sep 2026</td><td>-</td><td>12.0</td><td>0.0</td><td>12.0</td></tr></table></html>'''


def main() -> None:
    blackrock = parse_blackrock(BLACKROCK)
    assert blackrock["reference_date"] == "2026-09-04"
    assert blackrock["holdings_btc"] == 785635.3173
    farside = parse_farside(FARSIDE, today=date(2026, 9, 5))
    assert len(farside["records"]) == 2
    assert farside["records"][0]["flows_usd_millions"]["IBIT"] == 100.5
    assert farside["records"][0]["flows_usd_millions"]["GBTC"] == -20.0
    assert farside["records"][1]["flows_usd_millions"]["IBIT"] is None
    assert farside["records"][1]["quality"] == "RECENT_MAY_REVISE"
    print("ETF DATA BRIDGE SELF-TEST: PASSED")


if __name__ == "__main__":
    main()
