"""Trade management (§7) in candle-only (OHLC) mode."""

from __future__ import annotations

import pytest

from helpers import DAY
from imbalance.calendar import SessionRules
from imbalance.config import AppConfig, SessionConfig
from imbalance.costs import fill_costs, slipped
from imbalance.engine.risk import RiskBook, TradePlan, compute_plan
from imbalance.engine.trades import TradeManager
from imbalance.models import Candle, Instrument, Kind, mirror
from imbalance.timeutil import at

CFG = AppConfig.from_dict({})
P = CFG.params
SESSION = SessionRules(SessionConfig())
INST = Instrument(token=7, symbol="ABC", underlying="ABC", kind=Kind.EQ, tick_size=0.05)


def candle(hhmm, o, h, l, c, turnover=0.5, buy=0.5, vwap=None):
    vol = 10_000
    return Candle(token=7, start=at(DAY, hhmm), open=o, high=h, low=l, close=c, turnover_cr=turnover, volume=vol,
                  buy_vol=int(vol * buy), sell_vol=vol - int(vol * buy), vwap=vwap, closed=True, n_ticks=30)


def open_trade(direction=1, entry=100.0, stop=99.5, s2_high=100.2, qty=1000, start="10:00"):
    tm = TradeManager(lambda: CFG, lambda tr, ts: [])
    R = entry - stop
    plan = TradePlan(direction=direction, entry=entry, stop=stop, R=R, risk_pct=R / abs(entry) * 100, qty=qty,
                     lots=None, t1=entry + 1.5 * R, t2=entry + 3 * R, position_value=qty * abs(entry),
                     entry_mode="close")
    tr, _ = tm.open(setup_id="S", plan=plan, inst=INST, ts=at(DAY, start), candle_start=at(DAY, start),
                    s2_high_view=s2_high, s1_turnover=5.0, shadow=False, suppressed=None, grade="A", score=80,
                    params=P, cash_fut=False, tags=[])
    return tm, tr


def feed(tm, cs, hist=None):
    events = []
    hist = list(hist or [])
    for c in cs:
        hist.append(c)
        events += tm.on_candle(c, hist, P, SESSION, "ohlc", c.end + 1.5)
    return events


def test_t1_then_t2():
    tm, tr = open_trade()
    feed(tm, [candle("10:01", 100.0, 100.8, 99.9, 100.7), candle("10:02", 100.7, 101.6, 100.6, 100.65)])
    assert tr.status == "closed" and tr.exit_reason == "t2"
    assert [x["reason"] for x in tr.exits] == ["t1", "t2"]
    assert tr.gross_r == pytest.approx(2.25)


def test_breakeven_after_t1():
    tm, tr = open_trade()
    feed(tm, [candle("10:01", 100.0, 100.8, 99.9, 100.7), candle("10:02", 100.7, 100.75, 99.95, 100.0)])
    assert tr.exit_reason == "breakeven_stop" and tr.gross_r == pytest.approx(0.75)


def test_stop_first_when_candle_spans_both():
    tm, tr = open_trade()
    feed(tm, [candle("10:01", 100.0, 101.0, 99.4, 100.9)])       # green: O -> L (stop) -> H
    assert tr.exit_reason == "stop" and tr.gross_r == pytest.approx(-1.0)


def test_gap_through_stop_fills_at_open():
    tm, tr = open_trade()
    feed(tm, [candle("10:01", 99.2, 99.3, 99.0, 99.1)])
    assert tr.exit_reason == "stop" and tr.exits[0]["price"] == pytest.approx(99.2)


def test_time_stop_after_10_candles_without_new_high():
    tm, tr = open_trade(s2_high=100.2)
    cs = [candle(f"10:{k:02d}", 100.0, 100.15, 99.9, 100.05) for k in range(1, 12)]
    feed(tm, cs)
    assert tr.exit_reason == "time_stop" and tr.candles_since_entry == 10
    tm, tr = open_trade(s2_high=100.2)
    cs = [candle("10:01", 100.0, 100.3, 99.9, 100.1)] + \
         [candle(f"10:{k:02d}", 100.0, 100.15, 99.9, 100.05) for k in range(2, 13)]
    feed(tm, cs)
    assert tr.is_active                                             # a new high disarms the time stop


def test_end_of_day_exit_for_cash_at_1510():
    tm, tr = open_trade(start="15:00", s2_high=99.0)
    cs = [candle(f"15:{k:02d}", 100.0, 100.4, 99.8, 100.1) for k in range(1, 12)]
    feed(tm, cs)
    assert tr.exit_reason == "eod" and tr.exits[-1]["ts"] == at(DAY, "15:10") + 1.5


def test_sell_surge_exit_warning():
    tm, tr = open_trade(s2_high=99.0)
    ev = feed(tm, [candle("10:01", 100.2, 100.25, 99.7, 99.75, turnover=3.5, buy=0.3)])
    assert tr.exit_reason == "sell_surge"
    assert [e.alert for e in ev if e.alert] == ["EXIT_WARNING", "EXIT"]


def test_trailing_stop_after_t1():
    tm, tr = open_trade(s2_high=99.0)
    hist = []
    # 10:00 entry candle; 3-minute buckets are aligned to 09:15: 10:00-10:02, 10:03-10:05 ...
    feed(tm, [candle("10:01", 100.0, 100.8, 99.9, 100.7, vwap=99.8)], hist)
    assert tr.status == "t1" and tr.stop == pytest.approx(100.0)
    hist = [candle("10:00", 99.9, 100.1, 99.8, 100.0), candle("10:01", 100.0, 100.8, 99.9, 100.7, vwap=99.8)]
    feed(tm, [candle("10:02", 100.7, 100.9, 100.4, 100.85, vwap=99.9)], hist)
    # bucket 10:00-10:02 complete: low 99.8, VWAP 99.9 -> trail level below current stop, unchanged
    assert tr.stop == pytest.approx(100.0)
    hist += [candle("10:02", 100.7, 100.9, 100.4, 100.85, vwap=99.9)]
    seq = [candle("10:03", 100.85, 101.0, 100.6, 100.95, vwap=100.1),
           candle("10:04", 100.95, 101.1, 100.7, 101.0, vwap=100.2),
           candle("10:05", 101.0, 101.2, 100.65, 101.1, vwap=100.3)]
    feed(tm, seq, hist)
    assert tr.stop == pytest.approx(100.6 - 0.05)                # max(low of 10:03-10:05, VWAP) - 1 tick


def test_short_trade_in_view_space():
    tm, tr = open_trade(direction=-1, entry=-100.0, stop=-100.5, s2_high=-99.8)   # real: short 100, stop 100.5
    real = [candle("10:01", 100.0, 100.1, 99.2, 99.3), candle("10:02", 99.3, 99.4, 98.4, 98.5)]
    feed(tm, real)
    assert tr.exit_reason == "t2" and tr.gross_r == pytest.approx(2.25)
    row = tr.row()
    assert row["entry"] == 100.0 and row["stop"] == 100.0 and row["t1"] == pytest.approx(99.25)
    assert tr.gross_pnl > 0 and tr.net_pnl < tr.gross_pnl


def test_plan_rounding_and_lots():
    fut_inst = Instrument(token=8, symbol="ABCFUT", underlying="ABC", kind=Kind.FUT, lot_size=5500, tick_size=0.01)
    plan = compute_plan(162.35, 161.65, 161.0, 1.0, P, fut_inst, 1)
    assert plan.stop == pytest.approx(161.40) and plan.R == pytest.approx(0.95)
    assert plan.t1 == pytest.approx(163.78) and plan.t2 == pytest.approx(165.20)   # the spec's example
    assert plan.qty == 0 and plan.lots == 0 and plan.note
    rich = P.model_copy(update={"capital": 2_000_000})
    plan = compute_plan(162.35, 161.65, 161.0, 1.0, rich, fut_inst, 1)
    assert plan.qty == 5500 and plan.lots == 1


def test_costs_and_slippage():
    t = CFG.backtest.costs["EQ"]
    c = fill_costs([("buy", 100_000), ("sell", 101_000)], t)
    brokerage, stt, exch, sebi, stamp = 40.0, 25.25, 201_000 * 0.00297 / 100, 201_000 * 0.0001 / 100, 3.0
    assert c == pytest.approx(brokerage + stt + exch + sebi + stamp + 0.18 * (brokerage + exch + sebi))
    assert slipped(100.0, "buy", False, 0.05, CFG.backtest) == pytest.approx(100.05)
    assert slipped(100.0, "sell", True, 0.05, CFG.backtest) == pytest.approx(100.0 - 0.05 - 0.03)


def test_risk_book_guardrails():
    b = RiskBook()
    b.record(-100)
    b.record(-100)
    assert b.tripped(P) is None
    b.record(-100)
    assert b.tripped(P) == "max_daily_losses"
    b2 = RiskBook()
    b2.record(-15_000)
    assert b2.tripped(P) == "max_daily_loss"


def test_mirror_roundtrip():
    c = candle("10:00", 100, 101, 99, 100.5, buy=0.7)
    m = mirror(c)
    assert (m.open, m.high, m.low, m.close) == (-100, -99, -101, -100.5)
    assert m.buy_vol == c.sell_vol and m.red and c.green
