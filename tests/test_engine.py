from dataclasses import replace
from decimal import Decimal as D
import unittest

from manual_tracker.config import Config
from manual_tracker.engine import InvalidData, break_even, evaluate, metrics, number, validate_details
from tests.helpers import Clock, details, observation


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.cfg = Clock(), Config()

    def position(self, direction="LONG", **changes):
        d = details(direction)
        p = dict(details=d, recommended_stop=d["initial_stop"], best_price="100", activated=False, stage="WAITING_1R", last_price_at=None)
        p.update(changes)
        return p

    def test_invalid_numbers(self):
        for value in ["NaN", "Infinity", "-Infinity", "-1", "0", "", None, True, "1e9999", "1e-9999", "1,23"]:
            with self.subTest(value=value), self.assertRaises(InvalidData):
                number(value)

    def test_long_short_registration_validation(self):
        for direction in ["LONG", "SHORT"]:
            self.assertEqual(validate_details(details(direction), direction)["execution_source"], "USER_REPORTED")

    def test_invalid_quantity_entry_and_stop(self):
        for key, value in [("quantity", "0"), ("entry", "NaN"), ("initial_stop", "100"), ("initial_stop", "110"), ("entry_fee", None), ("exit_fee", "1")]:
            with self.subTest(key=key, value=value), self.assertRaises(InvalidData):
                validate_details({**details(), key: value}, "LONG")
        with self.assertRaises(InvalidData):
            validate_details({**details("SHORT"), "initial_stop": "99"}, "SHORT")

    def test_unsupported_and_spot_short_rejected(self):
        for d in [details(instrument="INVERSE"), details("SHORT", "SPOT"), {**details(), "multiplier": "100"}, {**details(), "symbol": "BTCUSD"}]:
            with self.assertRaises(InvalidData):
                validate_details(d, d["direction"])
        with self.assertRaises(InvalidData):
            validate_details(details(), "WAIT")

    def test_long_pnl_risk_and_roi(self):
        m = metrics(details(), "110")
        self.assertEqual(m["gross_pnl"], D(20))
        self.assertEqual(m["net_pnl"], D("19.58"))
        self.assertEqual(m["initial_risk"], D(20))
        self.assertEqual(m["r_multiple"], D(1))
        self.assertEqual(m["estimated_roi_pct"], D("48.95"))

    def test_short_pnl(self):
        m = metrics(details("SHORT"), "90")
        self.assertEqual(m["net_pnl"], D("19.62"))
        self.assertEqual(m["r_multiple"], D(1))

    def test_linear_multiplier_accounting(self):
        m = metrics({**details(), "multiplier": "10"}, "110")
        self.assertEqual(m["gross_pnl"], D(200))
        self.assertEqual(m["initial_risk"], D(200))

    def test_missing_leverage_disables_roi(self):
        self.assertIsNone(metrics({**details(), "leverage": None}, "110")["estimated_roi_pct"])

    def test_spot_long_accounting(self):
        m = metrics(details(instrument="SPOT"), "110")
        self.assertEqual(m["estimated_roi_pct"], D("9.79"))

    def test_inverse_cannot_use_linear_pnl(self):
        with self.assertRaises(InvalidData):
            metrics(details(instrument="INVERSE"), "110")

    def test_fee_adjusted_break_even_both_directions(self):
        for direction, expected in [("LONG", "100.26"), ("SHORT", "99.75")]:
            d = details(direction)
            be = break_even(d, D("0.01"), D(5))
            self.assertEqual(be, D(expected))
            self.assertGreaterEqual(metrics(d, be)["net_pnl"], D("0.1"))

    def test_before_activation_no_alert(self):
        out = evaluate(self.position(), observation(self.clock, "109"), self.cfg, self.clock())
        self.assertIsNone(out["event"])

    def test_activation_exactly_one_r(self):
        for direction, price in [("LONG", "110"), ("SHORT", "90")]:
            out = evaluate(self.position(direction), observation(self.clock, price), self.cfg, self.clock())
            self.assertEqual(out["event"], "BREAK_EVEN")
            self.assertTrue(out["activated"])

    def test_configurable_activation(self):
        out = evaluate(self.position(), observation(self.clock), replace(self.cfg, activation_r=D(2)), self.clock())
        self.assertIsNone(out["event"])

    def test_atr_long_short(self):
        for direction, price, old, expected in [("LONG", "120", "100.26", "116"), ("SHORT", "80", "99.75", "84")]:
            p = self.position(direction, activated=True, recommended_stop=old)
            out = evaluate(p, observation(self.clock, price), self.cfg, self.clock())
            self.assertEqual(out["event"], "ATR_TRAIL")
            self.assertEqual(out["stop"], D(expected))

    def test_stop_never_moves_backwards(self):
        for direction, price, old in [("LONG", "120", "117"), ("SHORT", "80", "83")]:
            p = self.position(direction, activated=True, recommended_stop=old)
            out = evaluate(p, observation(self.clock, price, atr="10"), self.cfg, self.clock())
            self.assertEqual(out["stop"], D(old))
            self.assertIsNone(out["event"])

    def test_minimum_improvement(self):
        p = self.position(activated=True, recommended_stop="115.9")
        out = evaluate(p, observation(self.clock, "120"), self.cfg, self.clock())
        self.assertIsNone(out["event"])

    def test_tick_rounding_safety(self):
        p = self.position(activated=True, recommended_stop="100.26")
        out = evaluate(p, observation(self.clock, "120.004", atr="1.997"), self.cfg, self.clock())
        self.assertEqual(out["stop"], D("116.01"))
        self.assertLess(out["stop"], D("120.004"))

    def test_stop_touch_is_advisory(self):
        out = evaluate(self.position(), observation(self.clock, "89"), self.cfg, self.clock())
        self.assertEqual(out["event"], "STOP_TOUCHED")
        self.assertEqual(out["stop"], D(90))

    def test_stale_future_nan_wrong_reference_rejected(self):
        for changes in [{"timestamp": self.clock()-21}, {"timestamp": self.clock()+3}, {"timestamp": float("nan")}, {"atr_timestamp": self.clock()-81}, {"reference": "LAST"}, {"atr": D("NaN")}]:
            with self.subTest(changes=changes), self.assertRaises(InvalidData):
                evaluate(self.position(), observation(self.clock, **changes), self.cfg, self.clock())

    def test_out_of_order_rejected(self):
        with self.assertRaises(InvalidData):
            evaluate(self.position(last_price_at=self.clock()+1), observation(self.clock), self.cfg, self.clock())

    def test_impossible_break_even_not_recommended(self):
        p = self.position()
        p["details"].update(entry_fee="0.09", exit_fee="0.09")
        self.assertIsNone(evaluate(p, observation(self.clock), self.cfg, self.clock())["event"])
