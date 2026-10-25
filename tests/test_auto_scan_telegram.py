import pytest
from futures.models import Result
from futures.strategy import evaluate_auto
from futures.telegram_alert import (
    dispatch_valid_trade_alert,
    format_telegram_alert,
    load_telegram_config,
    should_send_telegram_alert,
    trading_type_label,
)
from tests.fixtures import setup


def test_trading_type_labels():
    assert trading_type_label("SCALPING") == "SCALPER"
    assert trading_type_label("INTRADAY") == "INTRADAY"
    assert trading_type_label("SWING") == "SWING"


def test_evaluate_auto_produces_valid_setup_on_fixture():
    snap, account = setup()
    res = evaluate_auto(snap, account)
    assert res.status == "VALID"
    assert res.direction == "LONG"
    assert res.plan is not None
    assert res.plan["liquidation_buffer"] >= res.plan["atr"]


@pytest.mark.parametrize("mode,expected_label", [
    ("SCALPING", "SCALPER"),
    ("INTRADAY", "INTRADAY"),
    ("SWING", "SWING"),
])
def test_telegram_sent_only_for_buy_or_sell_never_wait(tmp_path, mode, expected_label):
    db_path = tmp_path / "test_tg.sqlite3"
    snap, account = setup()
    res = evaluate_auto(snap, account)
    assert res.status == "VALID"
    res.plan["mode"] = mode
    res.meta["mode"] = mode

    sent_payloads = []
    class DummyResp:
        status_code = 200
        text = "ok"
        def json(self):
            return {"ok": True}

    class DummySession:
        def post(self, url, json=None, timeout=10):
            sent_payloads.append((url, json))
            return DummyResp()

    # 1. WAIT must NEVER be sent to Telegram
    wait_outcome = dispatch_valid_trade_alert(
        db_path, res, "WAIT", {"ok": True, "decision": "WAIT", "reasons": ["Wait"]},
        "TOKEN123", "CHAT123", session=DummySession()
    )
    assert wait_outcome["sent"] is False
    assert wait_outcome["skipped"] is True
    assert len(sent_payloads) == 0

    # 2. NO_TRADE must NEVER be sent to Telegram
    no_trade_outcome = dispatch_valid_trade_alert(
        db_path, res, "NO_TRADE", {"ok": True, "decision": "NO_TRADE", "reasons": ["No trade"]},
        "TOKEN123", "CHAT123", session=DummySession()
    )
    assert no_trade_outcome["sent"] is False
    assert no_trade_outcome["skipped"] is True
    assert len(sent_payloads) == 0

    # 3. VALID LONG (BUY) MUST be sent to Telegram with [SCALPER]/[INTRADAY]/[SWING] label
    valid_ai = {"ok": True, "decision": res.direction, "reasons": ["Konfluensi EMA & VWAP valid."], "slot": 2, "model": "openai/gpt-oss-20b"}
    buy_outcome = dispatch_valid_trade_alert(
        db_path, res, "VALID", valid_ai, "TOKEN123", "CHAT123", session=DummySession()
    )
    assert buy_outcome["sent"] is True
    assert buy_outcome["trading_type"] == expected_label
    assert len(sent_payloads) == 1
    msg_text = sent_payloads[0][1]["text"]
    assert f"[{expected_label}]" in msg_text
    assert "BUY / LONG" in msg_text

    # 4. Duplicate signal on the same closed candle must not spam Telegram again
    dup_outcome = dispatch_valid_trade_alert(
        db_path, res, "VALID", valid_ai, "TOKEN123", "CHAT123", session=DummySession()
    )
    assert dup_outcome["sent"] is False
    assert dup_outcome["skipped"] is True
    assert len(sent_payloads) == 1
