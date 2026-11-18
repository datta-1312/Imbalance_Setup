"""Synthetic 1-minute history for testing the backtest pipeline end to end.

Fictional instruments and prices. Some days carry an injected imbalance pattern
so every stage of the funnel is exercised; this says nothing about whether the
setup works on real markets.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

from ..calendar import TradingCalendar
from ..models import Instrument, Kind
from ..timeutil import at
from .data import InstrumentHistory, candles_from_rows

PROFILE_OPEN = 1.8


def _profile(m: int) -> float:
    t = (m - 555) / 385.0
    return 0.75 + PROFILE_OPEN * math.exp(-max(0, m - 555) / 12.0) + 0.9 * max(0.0, t - 0.75) / 0.25


def synthetic_history(n_stocks: int = 6, days: int = 30, start: date = date(2026, 9, 1), seed: int = 1,
                      with_futures: bool = True, pattern_prob: float = 0.35) -> dict[int, InstrumentHistory]:
    rng = random.Random(seed)
    cal = TradingCalendar()
    dates = []
    d = start
    while len(dates) < days:
        if cal.is_trading_day(d):
            dates.append(d)
        d += timedelta(days=1)
    out: dict[int, InstrumentHistory] = {}
    specs = []
    for i in range(n_stocks):
        und = f"SYN{i:02d}"
        eq = Instrument(token=500_000 + 2 * i, symbol=und, underlying=und, kind=Kind.EQ, tick_size=0.05)
        insts = [eq]
        if with_futures:
            fu = Instrument(token=500_001 + 2 * i, symbol=f"{und}FUT", underlying=und, kind=Kind.FUT, lot_size=500,
                            tick_size=0.05, exchange="NFO", series="current")
            eq.linked_token, fu.linked_token = fu.token, eq.token
            insts.append(fu)
        specs.append((insts, rng.uniform(150, 1500), rng.uniform(0.7, 1.5)))
    for insts, price0, b in specs:
        rows: dict[int, list[dict]] = {i.token: [] for i in insts}
        price = price0
        oi = rng.uniform(5e6, 2e7)
        for d in dates:
            pattern_at = rng.randint(600, 860) if rng.random() < pattern_prob else None
            for m in range(555, 940):
                mult, drift, noise = 1.0, rng.gauss(0, 0.03), 0.28
                if pattern_at is not None:
                    k = m - pattern_at
                    if k == 0:
                        mult, drift, noise = 9.0, 0.6, 0.05
                    elif k == 1:
                        mult, drift, noise = 1.3, 0.03, 0.05
                    elif 2 <= k <= 6:
                        mult, drift, noise = 1.0, -0.05, 0.05
                    elif k == 7:
                        mult, drift, noise = 6.5, 0.32, 0.05
                    elif 8 <= k <= 14:
                        mult, drift, noise = 1.6, rng.choice([0.12, -0.12]), 0.1
                o = price
                c = o * (1 + drift / 100 + rng.gauss(0, 0.0006))
                span = abs(c - o) + o * abs(rng.gauss(0, 0.0005))
                if mult > 3 and c > o:
                    h, l = c + span * 0.05, o - span * 0.05
                else:
                    h, l = max(o, c) + span * rng.uniform(0.1, 0.4), min(o, c) - span * rng.uniform(0.1, 0.4)
                turnover = b * _profile(m) * mult * math.exp(rng.gauss(0, noise) - noise ** 2 / 2)
                ts = at(d, m)
                for inst in insts:
                    f = 1.003 if inst.kind == Kind.FUT else 1.0
                    vol = int(turnover * (1.4 if inst.kind == Kind.FUT else 1.0) * 1e7 / (c * f))
                    if inst.kind == Kind.EQ and m >= 915:
                        continue
                    if inst.kind == Kind.FUT:
                        oi *= 1 + (0.003 if mult > 3 else rng.gauss(0, 0.0003))
                    rows[inst.token].append({"ts": ts, "open": round(o * f, 2), "high": round(h * f, 2),
                                             "low": round(l * f, 2), "close": round(c * f, 2), "volume": vol,
                                             "oi": int(oi) if inst.kind == Kind.FUT else None})
                price = c
        for inst in insts:
            out[inst.token] = InstrumentHistory(inst, candles_from_rows(inst.token, rows[inst.token]))
    return out
