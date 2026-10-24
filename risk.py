"""Optional manual account verification. Never fabricates liquidation or equity."""
from dataclasses import dataclass
from decimal import Decimal
import math
from strategy import quantize, DAY


@dataclass
class Account:
    equity: float = 0
    free_margin: float = 0
    risk_pct: float = .5
    leverage: int = 2
    taker_pct: float = 0
    exit_slippage_pct: float = 0
    funding_reserve_pct: float = 0
    active_count: int = 0
    open_risk: float = 0
    same_direction_alt_risk: float = 0
    drawdown_pct: float = 0
    loss_streak: int = 0
    liquidation: float = 0
    verified: bool = False
    news_clear: bool = False
    liquidity_ok: bool = False
    liquidation_verified: bool = False


def common_step(a, b):
    a, b = Decimal(str(a)), Decimal(str(b or a))
    scale = 10 ** max(-a.as_tuple().exponent, -b.as_tuple().exponent, 0)
    return float(Decimal(math.lcm(int(a * scale), int(b * scale))) / scale)


def verify(result, contract, account, now):
    a, p = account, result.levels
    out = {"ready": False, "reasons": [], "data": {}, "scenarios": []}
    why = out["reasons"]
    if not result.candidate or not p:
        why.append("Setup teknis belum lolos: " + "; ".join(result.reasons))
        return out
    if not 0 <= now - result.checked_at <= 30_000 or now >= p["expires_at"]:
        why.append("Snapshot kedaluwarsa; scan ulang")
        return out
    nums = [v for v in vars(a).values() if not isinstance(v, bool)]
    if any(not math.isfinite(v) or v < 0 for v in nums):
        why.append("Input akun/biaya harus angka nonnegatif yang valid")
        return out
    if a.equity <= 0 or a.free_margin <= 0 or not 0 < a.risk_pct <= .5 or not 1 <= a.leverage <= 3:
        why.append("Isi ekuitas/margin; risiko maksimal 0,5%, leverage 1–3x")
        return out
    if a.taker_pct >= 100 or a.exit_slippage_pct >= 100 or a.funding_reserve_pct >= 100:
        why.append("Persentase biaya tidak valid")
        return out
    if not a.verified:
        why.append("Akun, fee taker, funding, isolated dan auto-add margin belum diverifikasi")
    if not a.news_clear:
        why.append("Filter berita CPI/FOMC ±15 menit belum terverifikasi/terhalang")
    if not a.liquidity_ok:
        why.append("Likuiditas untuk kuantitas order belum diverifikasi")
    if not a.liquidation_verified or a.liquidation <= 0:
        why.append("Harga likuidasi dari bursa untuk ukuran/margin ini belum diverifikasi")
    if a.drawdown_pct >= 2 or a.loss_streak >= 3:
        why.append("Batas harian tercapai: tunggu hari UTC berikutnya")
    if a.active_count >= 2:
        why.append("Maksimal dua posisi/pending order")
    E, S, D, atr = p["entry"], p["stop"], p["distance"], p["atr"]
    fee, slip, fund = a.taker_pct / 100, a.exit_slippage_pct / 100, a.funding_reserve_pct / 100
    # Entry slippage already included in worst allowed stop-limit fill E.
    csl = E * fee + S * fee + E * (slip + fund)
    ctp = E * fee + p["tp1"] * fee + E * (slip + fund)
    net_rr = (abs(p["tp1"] - E) - ctp) / (D + csl)
    if csl > .2 * D + 1e-12:
        why.append("Biaya SL > 20% D")
    if net_rr < 1.5:
        why.append("RR bersih pembanding TP1 < 1,5")
    budget = min(a.equity * a.risk_pct / 100, a.equity * .01 - a.open_risk)
    if result.symbol != "BTCUSDT":
        budget = min(budget, a.equity * .005 - a.same_direction_alt_risk)
    if budget <= 0:
        why.append("Sisa anggaran risiko portofolio/kelompok altcoin habis")
        return out
    step = common_step(contract.step, contract.market_step)
    margin_cap = min(a.free_margin, .2 * a.equity)
    q = quantize(min(budget / (D + csl), margin_cap / (E / a.leverage + csl),
                     contract.max_qty, contract.market_max_qty), step)
    q1, q2 = quantize(Decimal(str(q)) / 2, step), quantize(Decimal(str(q)) / 4, step)
    q3 = float(Decimal(str(q)) - Decimal(str(q1)) - Decimal(str(q2)))
    minimum = max(contract.min_qty, contract.market_min_qty)
    if any(x < minimum or x <= 0 or x * min(E, S) < contract.min_notional for x in [q, q1, q2, q3]):
        why.append("Kuantitas / partial 50-25-runner di bawah minimum bursa")
    mark_deviation = abs(result.values["mark"] - result.values["last"])
    required_buffer = max(atr, mark_deviation + E * slip)
    buffer = S - a.liquidation if result.direction == "LONG" else a.liquidation - S
    if a.liquidation > 0 and buffer + 1e-12 < required_buffer:
        why.append("Buffer SL–likuidasi tidak memenuhi batas rancangan")
    sign = 1 if result.direction == "LONG" else -1
    # Break-even for the remaining position, allocating entry fee by remaining quantity.
    # q1 profit is deliberately not used to make a looser stop appear safe.
    be = E * (1 + fee + slip + fund) / (1 - fee) if sign == 1 else E * (1 - fee - slip - fund) / (1 + fee)
    be = quantize(be, contract.tick, up=sign == 1)
    out["data"] = dict(equity=a.equity, budget=budget, quantity=q, tp1_qty=q1, tp2_qty=q2,
        runner_qty=q3, actual_risk=q * (D + csl), notional=q * E, margin=q * E / a.leverage,
        cost_reserve=q * csl, csl=csl, net_rr=net_rr, leverage=a.leverage,
        liquidation=a.liquidation, liquidation_buffer=buffer, required_buffer=required_buffer,
        breakeven=be, entry=E, sl_distance=D, atr=atr)
    def pnl(quantity, exit_price):
        return quantity * (sign * (exit_price - E) - E * fee - exit_price * fee - E * (slip + fund))
    scenarios = [("SL awal", pnl(q, S)),
                 ("TP1 lalu sisa keluar di BE estimasi", pnl(q1, p["tp1"]) + pnl(q2 + q3, be)),
                 ("TP1 + TP2, runner keluar di TP1 (skenario)", pnl(q1 + q3, p["tp1"]) + pnl(q2, p["tp2"]))]
    for name, net in scenarios:
        out["scenarios"].append({"Skenario": name, "PnL bersih USDT": net,
                                  "% ekuitas": net / a.equity * 100,
                                  "ROI margin %": net / (q * E / a.leverage) * 100 if q else 0})
    out["ready"] = not why
    return out


def trailing_stop(direction, previous_stop, bars_after_tp2, current_atr, trigger_price):
    """Pure calculation; callers must supply only candles fully formed after TP2."""
    if not bars_after_tp2:
        return previous_stop, "HOLD"
    new = max(previous_stop, max(b["high"] for b in bars_after_tp2) - 2 * current_atr) if direction == "LONG" else min(previous_stop, min(b["low"] for b in bars_after_tp2) + 2 * current_atr)
    crossed = trigger_price <= new if direction == "LONG" else trigger_price >= new
    return new, "EXIT" if crossed else "UPDATE"
