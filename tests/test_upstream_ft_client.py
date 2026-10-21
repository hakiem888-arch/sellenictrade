"""
Upstream Freqtrade REST Client Test Suite (`tests/test_upstream_ft_client.py`).

Executes the upstream test cases from `ft_client/test_client/test_rest_client.py`
against `freqtrade_client.FtRestClient` using Python's standard `unittest` runner.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from requests.exceptions import ConnectionError as RequestsConnectionError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT / "ft_client") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "ft_client"))

from freqtrade_client import FtRestClient


def _get_rest_client():
    client = FtRestClient("http://localhost:8080", "freqtrader", "password")
    client._session = MagicMock()
    request_mock = MagicMock()
    client._session.request = request_mock
    return client, request_mock


class TestUpstreamFtRestClient(unittest.TestCase):
    def test_FtRestClient_init(self) -> None:
        client = FtRestClient("http://localhost:8080", "freqtrader", "password")
        self.assertIsNotNone(client)
        self.assertEqual(client._serverurl, "http://localhost:8080")
        self.assertIsNotNone(client._session)
        self.assertEqual(client._session.auth, ("freqtrader", "password"))

    def test_FtRestClient_call_methods(self) -> None:
        for method in ("GET", "POST", "DELETE"):
            with self.subTest(method=method):
                client, mock = _get_rest_client()
                client._call(method, "/dummytest")
                self.assertEqual(mock.call_count, 1)
                getattr(client, f"_{method.lower()}")("/dummytest")
                self.assertEqual(mock.call_count, 2)

    def test_FtRestClient_call_invalid_and_connection_error(self) -> None:
        client, _ = _get_rest_client()
        with self.assertRaises(ValueError):
            client._call("PUTTY", "/dummytest")

        client._session.request = MagicMock(side_effect=RequestsConnectionError())
        res = client._call("GET", "/dummytest")
        self.assertIsNone(res)

    def test_FtRestClient_explicit_endpoints(self) -> None:
        cases = [
            ("start", [], {}),
            ("stop", [], {}),
            ("stopbuy", [], {}),
            ("reload_config", [], {}),
            ("balance", [], {}),
            ("count", [], {}),
            ("entries", [], {}),
            ("exits", [], {}),
            ("mix_tags", [], {}),
            ("locks", [], {}),
            ("lock_add", ["XRP/USDT", "2024-01-01 20:00:00Z", "*", "rand"], {}),
            ("delete_lock", [2], {}),
            ("daily", [], {}),
            ("daily", [15], {}),
            ("weekly", [], {}),
            ("monthly", [], {}),
            ("profit", [], {}),
            ("stats", [], {}),
            ("performance", [], {}),
            ("status", [], {}),
            ("version", [], {}),
            ("show_config", [], {}),
            ("ping", [], {}),
            ("logs", [], {}),
            ("logs", [55], {}),
            ("trades", [], {}),
            ("trades", [5], {}),
            ("trade", [1], {}),
            ("delete_trade", [1], {}),
            ("cancel_open_order", [1], {}),
            ("whitelist", [], {}),
            ("blacklist", [], {}),
            ("blacklist", ["XRP/USDT"], {}),
            ("forcebuy", ["XRP/USDT"], {}),
            ("forceenter", ["XRP/USDT", "short"], {}),
            ("forceenter", ["XRP/USDT", "short", 1.5], {"order_type": "market", "stake_amount": 100}),
            ("forceexit", [1], {}),
            ("strategies", [], {}),
            ("strategy", ["SampleStrategy"], {}),
            ("pairlists_available", [], {}),
            ("plot_config", [], {}),
            ("available_pairs", [], {}),
            ("pair_candles", ["XRP/USDT", "5m"], {}),
            ("pair_candles", ["XRP/USDT", "5m", 500], {"columns": ["close_time,close"]}),
            ("pair_history", ["XRP/USDT", "5m", "SampleStrategy"], {}),
            ("sysinfo", [], {}),
            ("health", [], {}),
        ]
        for method, args, kwargs in cases:
            with self.subTest(method=method, args=args, kwargs=kwargs):
                client, mock = _get_rest_client()
                executor = getattr(client, method)
                executor(*args, **kwargs)
                self.assertEqual(mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
