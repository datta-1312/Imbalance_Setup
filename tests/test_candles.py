from __future__ import annotations

import pytest

from imbalance.candles import CandleBuilder
from imbalance.flow import BUY, SELL, classify
from imbalance.models import Flag, Instrument, Kind, Tick
from imbalance.scenario import FeedEvent, TickScript
from imbalance.timeutil import at, minute_of_day

TOKEN = 101


def make_builder(session, kind=Kind.EQ):
    inst = Instrument(token=TOKEN, symbol="ABC", underlying="ABC", kind=kind)
    return CandleBuilder({TOKEN: inst}, session, stale_after_s=30)


def run(builder, records, end_ts):
    closed = []
    for r in records:
        t = r.recv_ts
        for b in builder.advance(t):
            closed.extend(b.candles.values())
        if isinstance(r, FeedEvent):
            (builder.on_disconnect if r.kind == "disconnect" else builder.on_reconnect)(r.ts)
            continue
        _, batches = builder.on_tick(r)
        for b in batches:
            closed.extend(b.candles.values())
        builder.check_stale(t)
    for b in builder.advance(end_ts):
        closed.extend(b.candles.values())
    return closed


def test_classifier_quote_then_tick_rule():
    assert classify(101.0, 100.9, 100.9, 101.0, 0) == BUY        # at the ask
    assert classify(100.9, 101.0, 100.9, 101.0, 0) == SELL       # at the bid
    assert classify(100.95, 100.9, 100.8, 101.1, 0) == BUY       # inside: uptick
    assert classify(100.85, 100.9, 100.8, 101.1, 0) == SELL      # inside: downtick
    assert classify(100.9, 100.9, 100.8, 101.1, SELL) == SELL    # unchanged: same as last


def test_turnover_from_cumulative_fields(day, session):
    s = TickScript(TOKEN, day, 100.0).preopen()
    s.candle("09:15", 100.0, 100.4, 99.9, 100.3, 2.0, buy_share=0.7)
    s.candle("09:16", 100.3, 100.5, 100.1, 100.2, 0.8, buy_share=0.3)
    b = make_builder(session)
    closed = run(b, s.records, at(day, "09:18"))
    assert [minute_of_day(c.start) for c in closed] == [555, 556]
    c1, c2 = closed
    # The opening auction volume (pre-open snapshot) is excluded from 09:15.
    assert c1.turnover_cr == pytest.approx(2.0, rel=0.01)
    assert c2.turnover_cr == pytest.approx(0.8, rel=0.01)
    assert c1.buy_share == pytest.approx(0.7, abs=0.05)
    assert c2.sell_share == pytest.approx(0.7, abs=0.05)
    assert (c1.open, c1.high, c1.low, c1.close) == (100.0, 100.4, 99.9, 100.3)
    assert not c1.flags & Flag.PARTIAL and not c1.flags & Flag.FALLBACK
    assert c1.vwap is not None and c1.closed


def test_first_snapshot_only_sets_baseline(day, session):
    """Started mid-day: the whole day's cumulative turnover must not land in one candle."""
    s = TickScript(TOKEN, day, 100.0, opening_volume=5_000_000)
    s.candle("11:00", 100.0, 100.2, 99.9, 100.1, 1.0)
    b = make_builder(session)
    closed = run(b, s.records, at(day, "11:02"))
    assert len(closed) == 1
    assert closed[0].flags & Flag.PARTIAL
    assert closed[0].turnover_cr == pytest.approx(1.0 * 29 / 30, rel=0.05)


def test_grace_period_and_next_minute_ticks(day, session):
    b = make_builder(session)
    base = at(day, "10:00")
    mk = lambda ts, v, p, recv: Tick(TOKEN, ts, p, v, p, recv_ts=recv)
    b.on_tick(mk(base - 1, 1000, 100.0, base - 0.5))           # baseline (09:59 minute -> partial)
    b.advance(base + 1.6)                                       # closes 09:59
    b.on_tick(mk(base + 10, 2000, 100.0, base + 10.2))          # 10:00 candle
    b.on_tick(mk(base + 60, 2500, 100.0, base + 60.3))          # 10:01 tick inside grace
    late = mk(base + 59, 2600, 100.0, base + 60.8)              # odd: older stamp, newer data
    b.on_tick(late)
    assert b.advance(base + 61.4) == []                         # before the deadline
    batches = b.advance(base + 61.5)
    c = batches[0].candles[TOKEN]
    assert c.start == base and c.volume == 1000                 # 10:01 snapshot not included
    nxt = b.forming(TOKEN)
    assert nxt.start == base + 60 and nxt.volume == 0 and nxt.v_end == 2600


def test_gap_affected_candle_and_day_turnover(day, session):
    """Scenario 13 at the builder level: 20 s silence + reconnect."""
    s = TickScript(TOKEN, day, 100.0).preopen()
    s.quiet("09:15", 15, turnover_cr=0.6)
    s.feed_event("disconnect", "09:30", 20.5)
    s.candle("09:30", 100.0, 100.6, 99.9, 100.5, 5.0, buy_share=0.7, skip=(20, 40))
    s.feed_event("reconnect", "09:30", 40.5)
    s.quiet("09:31", 3, turnover_cr=0.6)
    b = make_builder(session)
    closed = run(b, s.records, at(day, "09:35"))
    by_min = {minute_of_day(c.start): c for c in closed}
    gap = by_min[570]
    assert gap.flags & Flag.GAP
    assert not by_min[569].flags & Flag.GAP and not by_min[571].flags & Flag.GAP
    assert gap.turnover_cr == pytest.approx(5.0, rel=0.01)       # gap trades still counted
    first = s.records[0]
    last = [r for r in s.records if isinstance(r, Tick)][-1]
    day_total = (last.atp * last.volume - first.atp * first.volume) / 1e7
    assert sum(c.turnover_cr for c in closed) == pytest.approx(day_total, rel=1e-6)
    assert not b.stale_tokens()


def test_stale_after_30s_silence(day, session):
    s = TickScript(TOKEN, day, 100.0).preopen()
    s.quiet("10:00", 2)
    b = make_builder(session)
    run(b, s.records, at(day, "10:02"))
    b.check_stale(at(day, "10:02", 40))
    st = b.drain_status()
    assert [(x.token, x.status) for x in st] == [(TOKEN, "stale")]
    # fresh data clears the flag and the next candle is gap-affected
    t = Tick(TOKEN, at(day, "10:03", 5), 100.0, s.v + 100, 100.0, recv_ts=at(day, "10:03", 5.2))
    b.advance(t.recv_ts)
    b.on_tick(t)
    assert [x.status for x in b.drain_status()] == ["fresh"]
    assert b.forming(TOKEN).flags & Flag.GAP


def test_implausible_turnover_falls_back(day, session):
    b = make_builder(session)
    base = at(day, "10:00")
    b.on_tick(Tick(TOKEN, base - 5, 100.0, 1_000_000, 100.00, recv_ts=base - 5))
    b.on_tick(Tick(TOKEN, base + 5, 100.1, 1_010_000, 100.00, recv_ts=base + 5,
                   bid_px=(100.0,), ask_px=(100.2,)))
    # ATP drops far more than the trades allow -> negative cumulative difference
    b.on_tick(Tick(TOKEN, base + 30, 100.1, 1_020_000, 99.00, recv_ts=base + 30,
                   bid_px=(100.0,), ask_px=(100.2,)))
    c = {bt.minute_start: bt for bt in b.advance(base + 62)}[base].candles[TOKEN]
    assert c.flags & Flag.FALLBACK
    assert c.turnover_cr == pytest.approx(20_000 * 100.1 / 1e7, rel=0.01)


def test_cash_closing_auction_and_futures_session(day, session):
    eq = Instrument(token=1, symbol="A", underlying="A", kind=Kind.EQ)
    fu = Instrument(token=2, symbol="AFUT", underlying="A", kind=Kind.FUT)
    b = CandleBuilder({1: eq, 2: fu}, session)
    closed = []
    for tok in (1, 2):
        b.on_tick(Tick(tok, at(day, "15:13", 30), 100.0, 1000, 100.0, recv_ts=at(day, "15:13", 30)))
    for tok in (1, 2):   # auction (cash) / continuous (futures)
        _, batches = b.on_tick(Tick(tok, at(day, "15:20", 10), 101.0, 5000, 100.5, recv_ts=at(day, "15:20", 10)))
        for batch in batches:
            closed.extend(batch.candles.values())
    for batch in b.advance(at(day, "15:45")):
        closed.extend(batch.candles.values())
    eq_minutes = sorted(minute_of_day(c.start) for c in closed if c.token == 1)
    fut_minutes = sorted(minute_of_day(c.start) for c in closed if c.token == 2)
    assert eq_minutes[-1] == 914                 # cash candles stop at 15:14
    assert fut_minutes[-1] == 939                # futures trade until 15:40
    assert all(c.turnover_cr == 0 for c in closed if c.token == 1)   # auction volume excluded


def test_block_window_flag(day, session):
    b = make_builder(session)
    b.on_tick(Tick(TOKEN, at(day, "14:05", 1), 100.0, 1000, 100.0, recv_ts=at(day, "14:05", 1)))
    _, early = b.on_tick(Tick(TOKEN, at(day, "14:20", 1), 100.0, 2000, 100.0, recv_ts=at(day, "14:20", 1)))
    out = {minute_of_day(c.start): c for bt in early + b.advance(at(day, "14:22")) for c in bt.candles.values()}
    assert out[845].flags & Flag.BLOCK_WINDOW
    assert out[859].flags & Flag.BLOCK_WINDOW
    assert not out[860].flags & Flag.BLOCK_WINDOW
