"""Acceptance scenarios 1–12 and 14 of spec §13, run end to end:
scripted snapshots -> candle builder -> setup engine (default parameters)."""

from __future__ import annotations

import json
import time

import pytest

from helpers import (DAY, EQ_TOKEN, FUT_TOKEN, by, config, eq, fut, link, processor, pullback, run, script,
                     surge1)
from imbalance.engine import State
from imbalance.timeutil import at


# --------------------------------------------------------------------------- 1
def test_s1_quiet_stock_provisional_then_confirmed():
    s = surge1(script())                                   # quiet ₹0.5 cr candles, B(m) = ₹0.625 cr
    proc = processor([eq()])
    t0 = time.perf_counter()
    events = run(proc, s, end="10:22:05")
    elapsed = time.perf_counter() - t0
    prov = by(events, alert="S1_PROVISIONAL")
    assert len(prov) == 1
    p = prov[0]
    assert at(DAY, "10:21") < p.ts < at(DAY, "10:22")                 # mid-candle
    assert 4.0 <= p.values["turnover_cr"] < 4.4                       # fired as running turnover crossed ₹4 cr
    conf = by(events, alert="S1_CONFIRMED")
    assert len(conf) == 1
    c = conf[0]
    close = at(DAY, "10:22") + 1.5                                    # boundary + grace = candle close
    assert c.ts - close <= 1.0
    assert c.to_state == "SURGE_1" and c.values["metrics"]["rvol"] == pytest.approx(8.0, rel=0.02)
    assert c.values["metrics"]["buy_share"] == pytest.approx(0.7, abs=0.05)
    assert proc.close_latency.max < 1.0 and elapsed < 5
    assert not by(events, alert="S1_CANCELLED")


# --------------------------------------------------------------------------- 2
def test_s2_large_cap_normal_turnover_no_alert():
    s = script(turnover=4.0)
    surge1(s, turnover=5.0)
    proc = processor([eq()], b_cr=3.85)                    # RVOL 5 / 3.85 = 1.3
    events = run(proc, s, end="10:23")
    assert not [e for e in events if e.alert]


# --------------------------------------------------------------------------- 3
def test_s3_red_candle_sell_share_no_long_setup():
    s = script()
    s.candle("10:21", 100.0, 100.05, 99.35, 99.40, 6.0, buy_share=0.25)
    events = run(processor([eq()]), s, end="10:23")
    assert not by(events, kind="transition")
    # short setup only if enabled
    s2 = script()
    s2.candle("10:21", 100.0, 100.05, 99.35, 99.40, 6.0, buy_share=0.25)
    events = run(processor([eq()], cfg=config(short_setup_enabled=True)), s2, end="10:23")
    conf = by(events, alert="S1_CONFIRMED")
    assert len(conf) == 1 and conf[0].direction == -1
    assert conf[0].values["s1"]["high"] == pytest.approx(100.05)      # real prices reported


# --------------------------------------------------------------------------- 4
def test_s4_provisional_cancelled_when_close_below_floor():
    s = script()
    # A corrupted ATP mid-candle pushes the running turnover over ₹4 cr; the
    # candle closes at ₹3.5 cr.
    s.candle("10:21", 100.0, 100.65, 99.95, 100.60, 3.5, buy_share=0.7, atp_glitch={20: 1.06})
    events = run(processor([eq()]), s, end="10:23")
    assert len(by(events, alert="S1_PROVISIONAL")) == 1
    cancel = by(events, alert="S1_CANCELLED")
    assert len(cancel) == 1
    assert cancel[0].values["failed"] == ["turnover_floor_cr"]
    assert cancel[0].values["turnover_cr"] == pytest.approx(3.5, rel=0.02)
    assert not by(events, alert="S1_CONFIRMED")


# --------------------------------------------------------------------------- 5
def test_s5_pullback_arms():
    s = pullback(surge1(script()), turnover=1.0)          # 6 candles, 40% retrace, 20% of S1
    proc = processor([eq()])
    events = run(proc, s, end="10:28:05")
    states = [(e.from_state, e.to_state) for e in by(events, kind="transition")]
    assert states == [("IDLE", "SURGE_1"), ("SURGE_1", "PULLBACK"), ("PULLBACK", "ARMED")]
    setup = proc.engine.trackers[EQ_TOKEN].setup
    assert setup.state == State.ARMED
    assert setup.retracement == pytest.approx(0.40, abs=0.005)
    assert setup.pb_ratio == pytest.approx(0.20, abs=0.01)
    assert setup.pb_candles == 6
    assert by(events, alert="ARMED")[0].grade in ("A", "B", "C")


# --------------------------------------------------------------------------- 6
def test_s6_distribution_candle_invalidates():
    s = pullback(surge1(script()), candles=[(100.60, 100.62, 100.50, 100.52), (100.52, 100.55, 100.45, 100.47)])
    s.candle("10:24", 100.47, 100.50, 100.40, 100.42, 4.0, buy_share=0.30)
    proc = processor([eq()])
    events = run(proc, s, end="10:25:05")
    inv = by(events, to_state="INVALIDATED")
    assert len(inv) == 1 and inv[0].reason == "distribution"
    assert inv[0].values["sell_share"] == pytest.approx(0.70, abs=0.05)
    assert proc.engine.trackers[EQ_TOKEN].setup is None


# --------------------------------------------------------------------------- 7
def test_s7_floor_broken_invalidates():
    s = pullback(surge1(script()), candles=[(100.60, 100.62, 100.50, 100.52), (100.52, 100.55, 100.45, 100.47)])
    s.candle("10:24", 100.47, 100.48, 99.85, 99.90, 1.0, buy_share=0.4)
    events = run(processor([eq()]), s, end="10:25:05")
    inv = by(events, to_state="INVALIDATED")
    assert len(inv) == 1 and inv[0].reason == "floor_broken"
    assert inv[0].values["floor"] == pytest.approx(99.95)


# --------------------------------------------------------------------------- 8
def test_s8_surge2_entry_with_trade_plan():
    s = pullback(surge1(script()))
    s.candle("10:28", 100.42, 100.72, 100.40, 100.70, 3.5, buy_share=0.7)
    proc = processor([eq()], b_overrides={628: 0.875})      # S2 RVOL = 3.5 / 0.875 = 4
    events = run(proc, s, end="10:29:05")
    entry = by(events, alert="ENTRY")
    assert len(entry) == 1
    e = entry[0]
    assert (e.from_state, e.to_state) == ("ARMED", "ENTRY")
    assert e.values["s2"]["rvol"] == pytest.approx(4.0, rel=0.02)
    plan = e.values["plan"]
    params = proc.engine.config.params
    atr = plan["atr"]
    L = 100.37
    import math
    stop = math.floor(round((L - params.stop_buffer_atr * atr) / 0.01, 6)) * 0.01
    assert plan["entry"] == pytest.approx(100.70)
    assert plan["stop"] == pytest.approx(stop, abs=1e-6)
    R = 100.70 - plan["stop"]
    assert plan["R"] == pytest.approx(R)
    assert plan["t1"] == pytest.approx(round(100.70 + 1.5 * R, 2), abs=0.011)
    assert plan["t2"] == pytest.approx(round(100.70 + 3.0 * R, 2), abs=0.011)
    assert plan["qty"] == int(params.capital * params.risk_per_trade_pct / 100 // R)
    assert 0.15 <= plan["risk_pct"] <= 1.2
    trade = proc.engine.trades.active[EQ_TOKEN]
    assert trade.status == "open" and not trade.shadow


# --------------------------------------------------------------------------- 9
def test_s9_armed_expires_after_45_minutes():
    s = pullback(surge1(script()))
    s.quiet("10:28", 50, turnover_cr=0.5)
    proc = processor([eq()])
    events = run(proc, s, end="11:18:05")
    exp = by(events, to_state="EXPIRED")
    assert len(exp) == 1 and exp[0].reason == "timeout"
    assert exp[0].ts == at(DAY, "11:06") + 1.5               # S1 10:21 + 45 min
    assert not by(events, alert="ENTRY")


# --------------------------------------------------------------------------- 10
def _early_and_late(cfg):
    s = script(until="09:16")
    s.candle("09:16", 100.0, 100.65, 99.95, 100.60, 5.0, buy_share=0.7)
    s.quiet("09:17", 30)
    late = script(token=EQ_TOKEN + 1, until="15:02")
    late.candle("15:02", 100.0, 100.65, 99.95, 100.60, 5.0, buy_share=0.7)
    insts = [eq(), eq(token=EQ_TOKEN + 1, und="XYZ")]
    return run(processor(insts, cfg=cfg), s, late, end="15:10")


def test_s10_no_setup_outside_session_window():
    events = _early_and_late(config(suddenness_min_candles=1))
    assert not by(events, alert="S1_CONFIRMED") and not by(events, alert="S1_PROVISIONAL")
    # Control: the same data does start setups when the windows are widened,
    # so the session window is what blocks them.
    events = _early_and_late(config(suddenness_min_candles=1, no_signal_before="09:15", last_surge1_time="15:05"))
    assert len(by(events, alert="S1_CONFIRMED")) == 2


# --------------------------------------------------------------------------- 11
def test_s11_banned_future_not_scanned_cash_scanned():
    e, f = eq(), fut(scannable=False, scan_block_reason="fo_ban", banned=True)
    link(e, f)
    se = surge1(script(EQ_TOKEN))
    sf = surge1(script(FUT_TOKEN, oi=1_000_000))
    events = run(processor([e, f]), se, sf, end="10:23")
    conf = by(events, alert="S1_CONFIRMED")
    assert [x.token for x in conf] == [EQ_TOKEN]
    assert not [x for x in events if x.token == FUT_TOKEN and x.alert]


# --------------------------------------------------------------------------- 12
def _fut_surge(oi_change):
    f = fut()
    s = surge1(script(FUT_TOKEN, oi=1_000_000), oi_change_pct=oi_change)
    events = run(processor([f]), s, end="10:23")
    return by(events, alert="S1_CONFIRMED")


def test_s12_short_covering_lowers_grade_and_tags():
    cov = _fut_surge(-1.0)
    up = _fut_surge(+1.0)
    assert len(cov) == 1 and len(up) == 1                    # alert raised either way
    assert cov[0].values["oi"]["reading"] == "covering"
    assert "short covering" in cov[0].values["tags"]
    assert up[0].values["oi"]["reading"] == "buildup"
    assert cov[0].score < up[0].score
    assert cov[0].values["grade_detail"]["penalties"].get("short_covering") == 10


# --------------------------------------------------------------------------- 14
def test_s14_replay_twice_identical_event_log():
    def once():
        e, f = eq(), fut()
        link(e, f)
        se = pullback(surge1(script(EQ_TOKEN)))
        se.candle("10:28", 100.42, 100.72, 100.40, 100.70, 3.5, buy_share=0.7)
        se.quiet("10:29", 30)
        sf = surge1(script(FUT_TOKEN, oi=2_000_000), oi_change_pct=1.0)
        sf.quiet("10:22", 37)
        events = run(processor([e, f], b_overrides={628: 0.875}), se, sf, end="15:45", finish=True)
        return [json.dumps(ev.to_record(), sort_keys=True) for ev in events]

    a, b = once(), once()
    assert a == b
    assert any('"ENTRY"' in line for line in a)
