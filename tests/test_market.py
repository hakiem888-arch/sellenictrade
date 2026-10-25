import pytest
from futures.market import FuturesMarket, MarketError, BASE
from tests.fixtures import setup, NOW


class Response:
    def __init__(self, data, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}

    def json(self):
        return self.data


class FakeBinanceSpot:
    def __init__(self):
        self.calls = []
        self.s, _ = setup()

    def get(self, url, params=None, **kwargs):
        assert url.startswith(BASE + "/api/v3/")
        self.calls.append(url)
        if url.endswith("/time"):
            data = {"serverTime": NOW}
        elif url.endswith("/exchangeInfo"):
            data = {"symbols": [{"symbol": "BTCUSDT", "status": "TRADING", "baseAsset": "BTC", "quoteAsset": "USDT", "isSpotTradingAllowed": True,
                     "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.01", "minPrice": "0.01", "maxPrice": "100000"},
                                 {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "10000"},
                                 {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.00000000", "minQty": "0.00000000", "maxQty": "10000"},
                                 {"filterType": "NOTIONAL", "minNotional": "5.0"}]}]}
        elif url.endswith("/klines"):
            interval = params["interval"]
            if interval == "1d":
                day = NOW // 86400000 * 86400000
                data = [[day - 86400000, "100", "110", "90", "105", "100", day - 1, "1", 1, "1", "1", "0"]]
            else:
                d = self.s.bias if interval == "4h" else self.s.execution
                data = [[int(r.open_time), str(r.open), str(r.high), str(r.low), str(r.close), str(r.volume), int(r.close_time), "1", 1, "1", "1", "0"] for _, r in d.iterrows()]
                step = 14400000 if interval == "4h" else 900000
                opened = NOW // step * step
                data.append([opened, "113.9", "113.99", "113.8", "113.9", "1", opened + step - 1, "1", 1, "1", "1", "0"])
        elif url.endswith("/depth"):
            data = {"lastUpdateId": 1, "bids": [["113.89", "100"]], "asks": [["113.91", "100"]]}
        elif url.endswith("/bookTicker"):
            data = {"symbol": "BTCUSDT", "bidPrice": "113.89", "askPrice": "113.91"}
        elif url.endswith("/price"):
            data = {"symbol": "BTCUSDT", "price": "113.9"}
        else:
            raise AssertionError(url)
        return Response(data)


def test_only_spot_closed_candles_and_contract_filters():
    fake = FakeBinanceSpot()
    snap = FuturesMarket(fake).load("BTCUSDT", "INTRADAY")
    assert len(snap.bias) >= 500 and len(snap.execution) >= 500
    assert snap.execution.close_time.max() < NOW
    assert snap.contract.tick == 0.01
    assert snap.contract.market_step == 0.001
    assert snap.contract.min_notional == 5.0
    assert snap.previous_day_high == 110
    assert snap.funding_interval_hours == 8.0
    assert snap.open_bar_high == 113.99
    assert all("/api/v3/" in x for x in fake.calls)


def test_rate_limit_blocks_followup_calls():
    class Limited:
        calls = 0
        def get(self, *args, **kwargs):
            self.calls += 1
            return Response({}, 429, {"Retry-After": "120"})
    session = Limited()
    market = FuturesMarket(session)
    for _ in range(2):
        with pytest.raises(MarketError):
            market.get("/api/v3/time")
    assert session.calls == 1


def test_metadata_and_closed_candles_cached_but_quotes_are_fresh():
    fake = FakeBinanceSpot()
    m = FuturesMarket(fake)
    first = m.load("BTCUSDT", "INTRADAY")
    count = len(fake.calls)
    first.execution.loc[0, "close"] = -999
    second = m.load("BTCUSDT", "INTRADAY")
    assert second.execution.loc[0, "close"] > 0
    assert fake.calls.count(BASE + "/api/v3/exchangeInfo") == 1
    assert len(fake.calls) - count < count
