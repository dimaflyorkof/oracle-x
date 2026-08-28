import requests
from datetime import datetime, timezone

BASE_URL = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"
TIMEOUT = 15

session = requests.Session()
session.headers.update({
    "User-Agent": "ORACLE-X/1.0",
    "Accept": "application/json",
})


def get_json(path, params=None):
    response = session.get(
        BASE_URL + path,
        params=params,
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


def fetch_funding():
    data = get_json(
        "/fapi/v1/fundingRate",
        {"symbol": SYMBOL, "limit": 1},
    )

    row = data[-1]

    ts_ms = int(row["fundingTime"])

    return {
        "timestamp": datetime.fromtimestamp(
            ts_ms / 1000,
            tz=timezone.utc,
        ).isoformat(),
        "timestamp_unix": ts_ms // 1000,
        "funding_rate": float(row["fundingRate"]),
    }


def fetch_open_interest():
    data = get_json(
        "/fapi/v1/openInterest",
        {"symbol": SYMBOL},
    )

    return float(data["openInterest"])


def fetch_long_short():
    data = get_json(
        "/futures/data/globalLongShortAccountRatio",
        {
            "symbol": SYMBOL,
            "period": "5m",
            "limit": 1,
        },
    )

    row = data[-1]

    return {
        "long_ratio": float(row["longAccount"]),
        "short_ratio": float(row["shortAccount"]),
        "long_short_ratio": float(row["longShortRatio"]),
    }


def fetch_taker():
    data = get_json(
        "/futures/data/takerlongshortRatio",
        {
            "symbol": SYMBOL,
            "period": "5m",
            "limit": 1,
        },
    )

    row = data[-1]

    return {
        "taker_buy_volume": float(row["buyVol"]),
        "taker_sell_volume": float(row["sellVol"]),
        "taker_ratio": float(row["buySellRatio"]),
    }


def fetch_snapshot():
    result = {
        "symbol": "BTC",
        "source": "binance_futures",
    }

    result.update(fetch_funding())

    result["open_interest"] = fetch_open_interest()

    result.update(fetch_long_short())
    result.update(fetch_taker())

    return result


if __name__ == "__main__":
    print("\nORACLE X - BINANCE FUTURES TEST\n")

    snapshot = fetch_snapshot()

    for key, value in snapshot.items():
        print(f"{key}: {value}")
