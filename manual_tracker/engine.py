"""Pure decimal accounting and deterministic, monotonically improving stops."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import re
import math


class InvalidData(ValueError):
    pass


def number(value, name="Angka", zero=False):
    try:
        if isinstance(value, bool) or len(str(value)) > 64:
            raise ValueError()
        n = Decimal(str(value))
        if not n.is_finite() or n < 0 or (n == 0 and not zero) or n > Decimal("1e18"):
            raise ValueError()
        if n != 0 and n < Decimal("1e-18"):
            raise ValueError()
        return n
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidData(f"{name} harus berupa angka finite {'nonnegatif' if zero else 'positif'}; gunakan titik desimal.") from None


def fmt(value):
    if value is None:
        return "N/A"
    return format(Decimal(str(value)).normalize(), "f")


def symbol_name(value):
    value = str(value).upper().replace("/", "")
    if not re.fullmatch(r"[A-Z0-9]{2,20}USDT", value):
        raise InvalidData("Hanya simbol Binance berkuotasi USDT yang didukung.")
    return value


def validate_details(data, direction):
    d = dict(data)
    if direction not in {"LONG", "SHORT"}:
        raise InvalidData("WAIT tidak dapat menjadi posisi.")
    d["symbol"] = symbol_name(d["symbol"])
    if d.get("instrument") not in {"SPOT", "LINEAR"}:
        raise InvalidData("Hanya SPOT dan Binance USDT perpetual LINEAR; inverse tidak didukung.")
    if d["instrument"] == "SPOT" and direction != "LONG":
        raise InvalidData("Spot hanya mendukung posisi LONG tanpa pinjaman.")
    for key in ("entry", "quantity", "initial_stop", "multiplier"):
        d[key] = fmt(number(d.get(key), key))
    # Binance spot/USD-M quantities are base units. Other contract sizes are
    # supported by the pure accounting function, but not by this venue adapter.
    if Decimal(d["multiplier"]) != 1:
        raise InvalidData("Binance adapter memakai kuantitas aset dasar dan multiplier 1.")
    risk = (Decimal(d["entry"]) - Decimal(d["initial_stop"])) * (1 if direction == "LONG" else -1)
    if risk <= 0:
        raise InvalidData("SL awal LONG harus di bawah entry; SL awal SHORT harus di atas entry.")
    for key in ("entry_fee", "exit_fee"):
        d[key] = fmt(number(d.get(key), key, zero=True))
        if Decimal(d[key]) >= Decimal("0.1"):
            raise InvalidData("Fee harus berupa fraksi < 0.1, misalnya 0.0004 untuk 0.04%.")
    if d.get("leverage") is not None:
        d["leverage"] = fmt(number(d["leverage"], "Leverage"))
        if not 1 <= Decimal(d["leverage"]) <= 125:
            raise InvalidData("Leverage harus 1..125, atau '-' jika tidak diketahui.")
    if d["instrument"] == "SPOT":
        d["leverage"] = None
    d["direction"] = direction
    d["execution_source"] = "USER_REPORTED"
    d["quote_asset"] = "USDT"
    return d


def metrics(d, price):
    p = number(price, "Harga")
    e, sl = number(d["entry"]), number(d["initial_stop"])
    if d["instrument"] not in {"SPOT", "LINEAR"} or (d["instrument"] == "SPOT" and d["direction"] != "LONG"):
        raise InvalidData("Akuntansi instrumen tidak didukung.")
    sign = Decimal(1 if d["direction"] == "LONG" else -1)
    risk = sign * (e - sl)
    if risk <= 0:
        raise InvalidData("Risiko awal harus positif.")
    size = number(d["quantity"]) * number(d["multiplier"])
    gross = sign * (p - e) * size
    fees = size * (e * number(d["entry_fee"], zero=True) + p * number(d["exit_fee"], zero=True))
    net = gross - fees
    margin = e * size if d["instrument"] == "SPOT" else (e * size / number(d["leverage"]) if d.get("leverage") else None)
    return {"gross_pnl": gross, "estimated_fees": fees, "net_pnl": net,
            "risk_per_unit": risk, "initial_risk": size * risk,
            "r_multiple": sign * (p - e) / risk,
            "estimated_roi_pct": net / margin * 100 if margin else None}


def round_tick(value, tick, up=False):
    return (value / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick


def break_even(d, tick, buffer_bps):
    e = number(d["entry"])
    fi, fo = number(d["entry_fee"], zero=True), number(d["exit_fee"], zero=True)
    buf = buffer_bps / Decimal(10000)
    if d["direction"] == "LONG":
        return round_tick(e * (1 + fi + buf) / (1 - fo), tick, up=True)
    return round_tick(e * (1 - fi - buf) / (1 + fo), tick)


@dataclass(frozen=True)
class Observation:
    price: Decimal
    timestamp: float
    atr: Decimal
    atr_timestamp: float
    tick: Decimal
    reference: str
    interval_seconds: int


def evaluate(position, obs, config, now):
    d = position["details"]
    p, tick, atr = number(obs.price), number(obs.tick), number(obs.atr)
    if not all(math.isfinite(t) for t in (now, obs.timestamp, obs.atr_timestamp)):
        raise InvalidData("INVALID_TIMESTAMP")
    if obs.timestamp > now + 2 or now - obs.timestamp > config.stale_seconds:
        raise InvalidData("STALE_PRICE")
    if obs.timestamp < float(position.get("last_price_at") or 0):
        raise InvalidData("OUT_OF_ORDER_PRICE")
    if obs.atr_timestamp > now or now - obs.atr_timestamp > obs.interval_seconds + config.stale_seconds:
        raise InvalidData("STALE_ATR")
    expected = "MARK" if d["instrument"] == "LINEAR" else "LAST"
    if obs.reference != expected:
        raise InvalidData("PRICE_REFERENCE_MISMATCH")
    old = number(position["recommended_stop"])
    if number(d["initial_stop"]) % tick != 0:
        raise InvalidData("INITIAL_STOP_OFF_TICK")
    long = d["direction"] == "LONG"
    best = number(position.get("best_price") or d["entry"])
    best = max(best, p) if long else min(best, p)
    m = metrics(d, p)
    out = {"metrics": m, "best_price": best, "stop": old,
           "activated": bool(position["activated"]), "event": None, "stage": position["stage"]}
    if (long and p <= old) or (not long and p >= old):
        out.update(event="STOP_TOUCHED", stage="STOP_TOUCHED")
        return out
    if not position["activated"]:
        if m["r_multiple"] < config.activation_r:
            return out
        candidate = break_even(d, tick, config.cost_buffer_bps)
        event = "BREAK_EVEN"
        minimum = tick
    else:
        candidate = round_tick(best - config.atr_multiplier * atr if long else best + config.atr_multiplier * atr, tick, up=not long)
        event = "ATR_TRAIL"
        minimum = max(tick * config.min_ticks, m["risk_per_unit"] * config.min_improvement_r)
    improvement = candidate - old if long else old - candidate
    valid_price = candidate <= p - tick if long else candidate >= p + tick
    if candidate > 0 and valid_price and improvement >= minimum:
        out.update(stop=candidate, activated=True, event=event, stage=event)
    return out
