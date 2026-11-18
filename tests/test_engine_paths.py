"""State-machine paths beyond the §13 table."""

from __future__ import annotations

import pytest

from helpers import (EQ_TOKEN, FUT_TOKEN, PULLBACK, by, config, eq, fut, link, processor, pullback, run, script,
                     surge1)
from imbalance.calendar import EventCalendar
from imbalance.engine import State
from helpers import DAY

S2 = (100.42, 100.72, 100.40, 100.70)
B_S2 = {628: 0.875}


def armed_script(token=EQ_TOKEN, oi=None):
    return pullback(surge1(script(token, oi=oi)))


def test_continuation_updates_h_and_s1_turnover():
    s = surge1(script())
    s.candle("10:22", 100.60, 101.10, 100.55, 101.05, 6.0, buy_share=0.7)
    proc = processor([eq()])
    events = run(proc, s, end="10:23:05")
    cont = by(events, reason="s1_continuation")
    assert len(cont) == 1
    st = proc.engine.trackers[EQ_TOKEN].setup
    assert st.H == pytest.approx(101.10) and st.s1_turnover == pytest.approx(6.0, rel=0.02)
    assert st.s1_low == pytest.approx(99.95)                 # floor unchanged


def test_new_high_during_pullback_resumes_surge1():
    s = surge1(script())
    s.candle("10:22", 100.60, 100.62, 100.50, 100.52, 1.0)
    s.candle("10:23", 100.52, 100.80, 100.50, 100.78, 1.0)
    proc = processor([eq()])
    events = run(proc, s, end="10:24:05")
    assert [e.reason for e in by(events, kind="transition")] == ["s1_confirmed", "pullback_started", "new_high_resumed"]
    st = proc.engine.trackers[EQ_TOKEN].setup
    assert st.state == State.SURGE_1 and st.H == pytest.approx(100.80)


def test_max_retrace_invalidates():
    s = surge1(script())
    s.candle("10:22", 100.60, 100.62, 100.50, 100.52, 1.0)
    s.candle("10:23", 100.52, 100.53, 100.20, 100.30, 1.0)      # (100.65-100.20)/0.70 = 64%
    events = run(processor([eq()]), s, end="10:24:05")
    inv = by(events, to_state="INVALIDATED")
    assert len(inv) == 1 and inv[0].reason == "max_retrace"


def test_futures_short_buildup_invalidates():
    f = fut()
    s = surge1(script(FUT_TOKEN, oi=1_000_000))
    s.candle("10:22", 100.60, 100.62, 100.45, 100.48, 2.0, buy_share=0.45, oi_change_pct=1.0)
    events = run(processor([f]), s, end="10:23:05")
    inv = by(events, to_state="INVALIDATED")
    assert len(inv) == 1 and inv[0].reason == "short_buildup"
    assert inv[0].values["oi_change_pct"] >= 0.5


def test_stale_data_invalidates_live_setup():
    s = surge1(script())
    events = run(processor([eq()]), s, end="10:22:40")
    inv = by(events, to_state="INVALIDATED")
    assert len(inv) == 1 and inv[0].reason == "stale_data"


def test_cooldown_blocks_new_surge_for_15_minutes():
    s = surge1(script())
    s.candle("10:22", 100.60, 100.62, 100.50, 100.52, 1.0)
    s.candle("10:23", 100.52, 100.53, 99.80, 99.85, 1.0)        # floor broken -> cooldown until 10:39
    s.quiet("10:24", 6)
    s.candle("10:30", 99.85, 100.50, 99.80, 100.45, 5.0, buy_share=0.7)   # would qualify, but cooling down
    s.quiet("10:31", 9)
    s.candle("10:40", 100.45, 101.10, 100.40, 101.05, 5.0, buy_share=0.7)  # after cooldown
    events = run(processor([eq()]), s, end="10:41:05")
    conf = by(events, alert="S1_CONFIRMED")
    assert [e.values["s1_time"] for e in conf] == ["10:21", "10:40"]


def test_risk_out_of_range_skips_entry():
    s = armed_script()
    s.candle("10:28", *S2, 3.5, buy_share=0.7)
    events = run(processor([eq()], cfg=config(max_risk_pct=0.3), b_overrides=B_S2), s, end="10:29:05")
    inv = by(events, to_state="INVALIDATED")
    assert len(inv) == 1 and inv[0].reason == "risk_out_of_range"
    assert not by(events, alert="ENTRY")


def test_guardrail_suppresses_entry_but_logs_it():
    s = armed_script()
    s.candle("10:28", *S2, 3.5, buy_share=0.7)
    proc = processor([eq()], b_overrides=B_S2)
    for _ in range(3):
        proc.engine.trades.book.record(-1000.0)
    events = run(proc, s, end="10:29:05")
    entry = by(events, to_state="ENTRY")
    assert len(entry) == 1 and entry[0].alert is None
    assert entry[0].values["suppressed"] == "max_daily_losses"
    trade = proc.engine.trades.active[EQ_TOKEN]
    assert trade.shadow
    snap = proc.engine.snapshot(0)
    assert not snap["entries"] and snap["archive"][0]["suppressed"] == "max_daily_losses"


def test_max_open_positions_suppresses():
    s = armed_script()
    s.candle("10:28", *S2, 3.5, buy_share=0.7)
    events = run(processor([eq()], cfg=config(max_open_positions=0), b_overrides=B_S2), s, end="10:29:05")
    assert by(events, to_state="ENTRY")[0].values["suppressed"] == "max_open_positions"


def test_provisional_s2_then_cancelled():
    s = armed_script()
    s.candle("10:28", 100.42, 100.80, 100.40, 100.50, 3.5, buy_share=0.7)   # closes low in its range
    events = run(processor([eq()], b_overrides=B_S2), s, end="10:29:05")
    assert len(by(events, alert="S2_PROVISIONAL")) == 1
    cancel = by(events, alert="S2_CANCELLED")
    assert len(cancel) == 1 and cancel[0].values["failed"] == ["green_close_in_top"]
    assert not by(events, alert="ENTRY")


def test_trade_t1_then_t2_from_ticks():
    s = armed_script()
    s.candle("10:28", *S2, 3.5, buy_share=0.7)
    s.candle("10:29", 100.70, 101.40, 100.65, 101.35, 1.0, buy_share=0.6)
    s.candle("10:30", 101.35, 101.95, 101.30, 101.90, 1.0, buy_share=0.6)
    proc = processor([eq()], b_overrides=B_S2)
    events = run(proc, s, end="10:31:05")
    trade_events = [e.reason for e in by(events, kind="trade")]
    assert trade_events[:1] == ["opened"]
    assert "t1" in trade_events and "t2" in trade_events and trade_events[-1] == "closed"
    tr = next(iter(proc.engine.trades.trades.values()))
    assert tr.status == "closed" and tr.exit_reason == "t2"
    assert tr.gross_r == pytest.approx(0.5 * 1.5 + 0.5 * 3.0, abs=0.05)
    assert tr.net_pnl < tr.gross_pnl
    setup = proc.engine.setups[tr.setup_id]
    assert setup.state == State.COMPLETED
    assert by(events, alert="T1") and by(events, alert="EXIT")


def test_trade_stop_loss_from_ticks():
    s = armed_script()
    s.candle("10:28", *S2, 3.5, buy_share=0.7)
    s.candle("10:29", 100.70, 100.71, 100.20, 100.25, 1.0, buy_share=0.4)
    proc = processor([eq()], b_overrides=B_S2)
    run(proc, s, end="10:30:05")
    tr = next(iter(proc.engine.trades.trades.values()))
    assert tr.exit_reason == "stop" and -1.3 < tr.gross_r <= -1.0
    assert proc.engine.trades.book.losers == 1


def test_mirrored_short_setup_end_to_end():
    m = lambda o, h, l, c: (200 - o, 200 - l, 200 - h, 200 - c)    # mirror image around 100
    s = script()
    s.candle("10:21", *m(100.0, 100.65, 99.95, 100.60), 5.0, buy_share=0.3)
    for k, cndl in enumerate(PULLBACK):
        s.candle(f"10:{22 + k}", *m(*cndl), 1.0, buy_share=0.55)
    s.candle("10:28", *m(*S2), 3.5, buy_share=0.3)
    proc = processor([eq()], cfg=config(short_setup_enabled=True), b_overrides=B_S2)
    events = run(proc, s, end="10:29:05")
    entry = by(events, alert="ENTRY")
    assert len(entry) == 1 and entry[0].direction == -1
    plan = entry[0].values["plan"]
    assert plan["entry"] == pytest.approx(99.30)
    assert plan["stop"] > plan["entry"] > plan["t1"] > plan["t2"]
    assert plan["stop"] >= 99.63                               # above the bounce high
    assert entry[0].values["s2"]["low"] == pytest.approx(99.28)


def test_cash_futures_confirmation_same_and_adjacent_minute():
    e, f = eq(), fut()
    link(e, f)
    se = surge1(script(EQ_TOKEN))
    sf = surge1(script(FUT_TOKEN, oi=1_000_000), oi_change_pct=1.0)
    events = run(processor([e, f]), se, sf, end="10:22:05")
    conf = {x.token: x for x in by(events, alert="S1_CONFIRMED")}
    assert conf[EQ_TOKEN].values["cash_fut"] and conf[FUT_TOKEN].values["cash_fut"]
    basis = conf[FUT_TOKEN].values["basis"]                  # §6: recorded, not used in rules
    assert basis["close_pct"] == pytest.approx(0.0, abs=0.05) and "change_bp" in basis
    # adjacent minute: the future surges one minute after the cash share
    se = surge1(script(EQ_TOKEN))
    se.candle("10:22", 100.60, 100.62, 100.55, 100.58, 0.5)
    sf = script(FUT_TOKEN, oi=1_000_000, until="10:22")
    sf.candle("10:22", 100.0, 100.65, 99.95, 100.60, 5.0, buy_share=0.7, oi_change_pct=1.0)
    proc = processor([e, f])
    events = run(proc, se, sf, end="10:23:05")
    conf = {x.token: x for x in by(events, alert="S1_CONFIRMED")}
    assert not conf[EQ_TOKEN].values["cash_fut"] and conf[FUT_TOKEN].values["cash_fut"]
    late = by(events, reason="cash_fut_confirmed_late")
    assert [x.token for x in late] == [EQ_TOKEN]
    assert proc.engine.trackers[EQ_TOKEN].setup.cash_fut["s1"]


def test_rollover_futures_need_cash_confirmation():
    e, f = eq(), fut(rollover=True)
    link(e, f)
    sf = surge1(script(FUT_TOKEN, oi=1_000_000), oi_change_pct=1.0)
    se = script(EQ_TOKEN, until="10:22")
    events = run(processor([e, f]), se, sf, end="10:22:05")
    assert not by(events, alert="S1_CONFIRMED")
    se = surge1(script(EQ_TOKEN))
    sf = surge1(script(FUT_TOKEN, oi=1_000_000), oi_change_pct=1.0)
    events = run(processor([e, f]), se, sf, end="10:22:05")
    assert {x.token for x in by(events, alert="S1_CONFIRMED")} == {EQ_TOKEN, FUT_TOKEN}


def test_event_day_modes():
    ev = EventCalendar()
    ev.add(DAY, "ABC", "results")
    tag = by(run(processor([eq()], events=ev), surge1(script()), end="10:22:05"), alert="S1_CONFIRMED")
    assert "event day" in tag[0].values["tags"]
    assert tag[0].values["grade_detail"]["penalties"]["event_day"] == 10
    sup = run(processor([eq()], cfg=config(event_day_mode="suppress"), events=ev), surge1(script()), end="10:22:05")
    assert not by(sup, alert="S1_CONFIRMED")
    req = run(processor([eq()], cfg=config(event_day_mode="require_a"), events=ev), surge1(script()), end="10:22:05")
    if tag[0].grade == "A":
        assert by(req, alert="S1_CONFIRMED")
    else:
        assert not by(req, alert="S1_CONFIRMED") and by(req, reason="s1_rejected_grade")


def test_buy_stop_mode_pending_then_filled_or_cancelled():
    s = armed_script()
    s.candle("10:28", *S2, 3.5, buy_share=0.7)
    s.candle("10:29", 100.70, 100.80, 100.65, 100.78, 1.0, buy_share=0.6)     # trades above S2 high + tick
    proc = processor([eq()], cfg=config(entry_mode="buy_stop"), b_overrides=B_S2)
    events = run(proc, s, end="10:30:05")
    entry = by(events, alert="ENTRY")[0]
    assert entry.values["plan"]["entry"] == pytest.approx(100.73) and entry.values["plan"]["entry_mode"] == "buy_stop"
    assert "filled" in [e.reason for e in by(events, kind="trade")]
    s = armed_script()
    s.candle("10:28", *S2, 3.5, buy_share=0.7)
    for k in range(3):                                                         # never reaches 100.73
        s.candle(f"10:{29 + k}", 100.68, 100.71, 100.60, 100.62, 0.5)
    proc = processor([eq()], cfg=config(entry_mode="buy_stop"), b_overrides=B_S2)
    events = run(proc, s, end="10:32:05")
    exp = by(events, to_state="EXPIRED")
    assert exp and exp[0].reason == "buy_stop_not_triggered"


def test_two_candle_surge_window():
    def build():
        s = script()
        s.candle("10:21", 100.0, 100.30, 99.95, 100.28, 3.5, buy_share=0.7)
        s.candle("10:22", 100.28, 100.65, 100.25, 100.60, 3.5, buy_share=0.7)
        return s
    assert not by(run(processor([eq()]), build(), end="10:23:05"), alert="S1_CONFIRMED")
    conf = by(run(processor([eq()], cfg=config(surge_window_candles=2)), build(), end="10:23:05"),
              alert="S1_CONFIRMED")
    assert len(conf) == 1 and conf[0].values["s1"]["window"] == 2
    assert conf[0].values["s1_time"] == "10:21"
    assert conf[0].values["s1"]["turnover_cr"] == pytest.approx(7.0, rel=0.02)


def test_block_window_tag_and_penalty():
    s = script(until="14:10")
    surge1(s, "14:10")
    conf = by(run(processor([eq()]), s, end="14:11:05"), alert="S1_CONFIRMED")
    assert "block window" in conf[0].values["tags"]
    assert conf[0].values["grade_detail"]["penalties"]["block_window"] == 10


def test_paused_symbol_not_scanned():
    proc = processor([eq()])
    proc.engine.set_paused(EQ_TOKEN, True)
    assert not by(run(proc, surge1(script()), end="10:22:05"), alert="S1_CONFIRMED")
