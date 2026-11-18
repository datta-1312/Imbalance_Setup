from __future__ import annotations

from datetime import date

import pytest

from imbalance.baseline import Baseline, BaselineStore, MinuteBar, build_baseline
from imbalance.calendar import EventCalendar, TradingCalendar
from imbalance.config import Params, UniverseConfig
from imbalance.instruments import build_universe, parse_ban_list, parse_instrument_master
from imbalance.models import Kind

MINUTES = list(range(555, 915))


def session(turnover_by_minute, default=1.0, price=100.0):
    return [MinuteBar(m, price, price + 0.2, price - 0.2, price, 10_000,
                      turnover_by_minute.get(m, default)) for m in MINUTES]


def test_same_minute_median_resists_spikes():
    sessions = [session({600: 1.0}) for _ in range(19)] + [session({600: 500.0})]
    b = build_baseline(1, sessions, MINUTES, smoothing=2)
    assert b.b(600) == pytest.approx(1.0)
    assert b.sessions == 20
    assert b.adtv_cr == pytest.approx((len(MINUTES) * 1.0 * 20 + 499.0) / 20)
    assert b.atr_seed == pytest.approx(0.4, rel=0.01)


def test_smoothing_pools_neighbouring_minutes():
    # minute 700 alone is high in every session, neighbours are low: the pooled
    # median over m±2 stays low; minutes far away are unaffected.
    sessions = [session({700: 9.0}) for _ in range(20)]
    b = build_baseline(1, sessions, MINUTES, smoothing=2)
    assert b.b(700) == pytest.approx(1.0)
    b0 = build_baseline(1, sessions, MINUTES, smoothing=0)
    assert b0.b(700) == pytest.approx(9.0)


def test_baseline_floor_and_roundtrip(tmp_path):
    b = build_baseline(7, [session({}, default=0.0)], MINUTES, floor_cr=0.01, asof=date(2026, 11, 18))
    assert b.b(600) == 0.01
    store = BaselineStore(tmp_path)
    store.save(date(2026, 11, 18), {7: b})
    loaded = store.load(date(2026, 11, 18))[7]
    assert loaded.b(600) == 0.01 and loaded.asof == date(2026, 11, 18)
    assert Baseline.flat(1, 0.5, MINUTES).b(700) == 0.5


MASTER = """instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,instrument_type,segment,exchange
1001,11,TATASTEEL,TATA STEEL,160.5,,0,0.01,1,EQ,NSE,NSE
2001,21,TATASTEEL26NOVFUT,TATASTEEL,161,2026-11-24,0,0.01,5500,FUT,NFO-FUT,NFO
2002,22,TATASTEEL26DECFUT,TATASTEEL,162,2026-12-29,0,0.01,5500,FUT,NFO-FUT,NFO
1002,12,SAIL,STEEL AUTHORITY,120,,0,0.01,1,EQ,NSE,NSE
2011,31,SAIL26NOVFUT,SAIL,121,2026-11-24,0,0.01,4700,FUT,NFO-FUT,NFO
3001,41,NIFTY26NOVFUT,NIFTY,26000,2026-11-24,0,0.1,75,FUT,NFO-FUT,NFO
3002,42,BANKNIFTY26NOVFUT,BANKNIFTY,58000,2026-11-24,0,0.2,35,FUT,NFO-FUT,NFO
3003,43,FINNIFTY26NOVFUT,FINNIFTY,27000,2026-11-24,0,0.1,65,FUT,NFO-FUT,NFO
4001,51,TATASTEEL26NOV150CE,TATASTEEL,10,2026-11-24,150,0.05,5500,CE,NFO-OPT,NFO
"""


def test_universe_ban_list_rollover_and_links():
    rows = parse_instrument_master(MASTER)
    cal = TradingCalendar()
    today = date(2026, 11, 18)   # Wed; expiry Tue 24 Nov -> sessions 18,19,20,23,24 = 5
    ban = parse_ban_list("Securities in Ban For Trade Date 18-NOV-2026:\n1,SAIL\n")
    assert ban == {"SAIL"}
    u = build_universe(rows, today, cal, ban, Params(), UniverseConfig(),
                       adtv_cr={1001: 300.0, 2001: 900.0, 2002: 40.0, 1002: 80.0, 2011: 60.0})
    ts_eq, ts_fut = u.instruments[1001], u.instruments[2001]
    assert ts_eq.kind == Kind.EQ and ts_fut.kind == Kind.FUT
    assert ts_eq.linked_token == 2001 and ts_fut.linked_token == 1001
    assert ts_fut.lot_size == 5500 and ts_fut.rollover and ts_eq.rollover
    # rollover window: next-month future subscribed but not scanned
    nxt = u.instruments[2002]
    assert nxt.series == "next" and not nxt.scannable and nxt.scan_block_reason == "next_month"
    # Scenario 11: stock in F&O ban -> future not scanned, cash share scanned
    assert not u.instruments[2011].scannable and u.instruments[2011].scan_block_reason == "fo_ban"
    assert u.instruments[1002].scannable
    # Index futures are regime-only, FINNIFTY not subscribed at all
    assert u.regime == {"BANKNIFTY": 3002, "NIFTY": 3001}
    assert not u.instruments[3001].scannable
    assert 3003 not in u.instruments and 4001 not in u.instruments
    # round trip
    from imbalance.instruments import Universe
    assert Universe.from_dict(u.to_dict()).instruments[2001].expiry == date(2026, 11, 24)


def test_liquidity_floor_and_missing_baseline():
    rows = parse_instrument_master(MASTER)
    u = build_universe(rows, date(2026, 11, 2), TradingCalendar(), set(), Params(), UniverseConfig(),
                       adtv_cr={1001: 10.0, 2001: 900.0})
    assert u.instruments[1001].scan_block_reason == "adtv_below_floor"
    assert u.instruments[1002].scan_block_reason == "no_baseline"
    assert u.instruments[2001].scannable
    assert not u.instruments[2001].rollover and 2002 not in u.instruments


def test_calendar_and_events(tmp_path):
    cal = TradingCalendar(holidays=[date(2026, 11, 19)])
    assert not cal.is_trading_day(date(2026, 11, 19))
    assert not cal.is_trading_day(date(2026, 11, 21))     # Saturday
    assert cal.previous_trading_days(date(2026, 11, 23), 2) == [date(2026, 11, 18), date(2026, 11, 20)]
    assert cal.sessions_to_expiry(date(2026, 11, 18), date(2026, 11, 24)) == 4
    f = tmp_path / "events.csv"
    f.write_text("date,symbol,type\n2026-11-18,TATASTEEL,results\n2026-11-18,SAIL,fo_exclusion\n")
    ev = EventCalendar.load(f)
    assert ev.is_event_day(date(2026, 11, 18), "TATASTEEL")
    assert ev.is_index_event(date(2026, 11, 18), "SAIL")
    assert not ev.is_event_day(date(2026, 11, 18), "SAIL")
