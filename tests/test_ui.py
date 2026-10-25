from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest
from futures.market import FuturesMarket, MarketError
from futures.groq_rotation import GroqRotation
from tests.fixtures import setup

APP = Path(__file__).resolve().parents[1] / "app.py"


def by_label(elements, label):
    return next(x for x in elements if x.label == label)


def app(monkeypatch, tmp_path):
    monkeypatch.setenv("FUTURES_HISTORY_DB", str(tmp_path / "history.sqlite3"))
    monkeypatch.setenv("GROQ_API_KEY_1", "gsk_test_key_1")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:TEST_BOT_TOKEN")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100987654321")
    return AppTest.from_file(str(APP), default_timeout=20).run()


def test_no_api_inputs_in_gui_and_auto_scan_intervals(monkeypatch, tmp_path):
    at = app(monkeypatch, tmp_path)
    assert not at.exception
    assert len([x for x in at.text_input if x.label.startswith("Key ")]) == 0
    interval_box = by_label(at.selectbox, "Interval Auto Scan (detik)")
    assert list(interval_box.options) == [30, 45, 60, 90]


def test_network_failure_becomes_wait(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise MarketError("HTTP 451")
    monkeypatch.setattr(FuturesMarket, "load", fail)
    at = app(monkeypatch, tmp_path)
    by_label(at.button, "Analisis").click().run()
    assert not at.exception
    assert any("WAIT" in x.value and "451" in x.value for x in at.error)
