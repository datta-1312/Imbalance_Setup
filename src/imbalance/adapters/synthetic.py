"""Synthetic NSE-like market for demos, load tests and UI development.

Seeded and deterministic. Each instrument gets a U-shaped intraday turnover
profile, a random-walk price with 5-level quotes, cumulative volume and
paise-rounded ATP like a broker snapshot, and OI for futures. From time to time
an imbalance pattern is injected into a stock (and its future): Surge 1 →
light-volume pullback → Surge 2, or variants that end in distribution or a
failed pullback, so every path of the state machine shows up on screen.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterable, Iterator

from ..baseline import Baseline
from ..instruments import Universe
from ..models import Instrument, Kind, Tick
from ..scenario import FeedEvent, Record
from ..timeutil import at, minute_of_day
from .base import MarketDataAdapter

# (minutes, turnover multiple, drift %/min, buy probability, OI %/min)
PATTERNS = {
    "full": [(1, 9.0, 0.60, 0.80, 0.30), (1, 1.4, 0.04, 0.55, 0.02), (5, 1.0, -0.05, 0.42, -0.04),
             (1, 6.5, 0.32, 0.80, 0.25), (8, 1.8, 0.10, 0.60, 0.05)],
    "failed_s2": [(1, 9.0, 0.60, 0.80, 0.30), (1, 1.4, 0.04, 0.55, 0.02), (5, 1.0, -0.05, 0.42, -0.04),
                  (1, 6.5, 0.32, 0.80, 0.25), (6, 2.0, -0.14, 0.38, -0.08)],
    "distribution": [(1, 9.0, 0.60, 0.80, 0.30), (1, 1.2, 0.02, 0.50, 0.0), (1, 6.5, -0.35, 0.22, 0.20),
                     (4, 1.5, -0.05, 0.45, 0.0)],
    "fade": [(1, 8.5, 0.55, 0.78, 0.25), (1, 1.2, 0.02, 0.5, 0.0), (6, 1.3, -0.16, 0.38, -0.05)],
}
PATTERN_WEIGHTS = [("full", 0.45), ("failed_s2", 0.2), ("distribution", 0.175), ("fade", 0.175)]


def intraday_profile(minute: int) -> float:
    """U-shaped turnover multiplier by IST minute of day."""
    t = (minute - 555) / 385.0          # 0 at 09:15, 1 at 15:40
    open_burst = 1.8 * math.exp(-max(0.0, minute - 555) / 12.0)
    close_ramp = 0.9 * max(0.0, t - 0.75) / 0.25
    return 0.75 + open_burst + close_ramp + 0.15 * math.cos(t * math.pi)


@dataclass
class _Sym:
    inst: Instrument
    rng: random.Random
    price: float
    b_cr: float
    tick: float
    v: int
    ct: float
    oi: int | None
    prev_close: float
    bid: float = 0.0
    ask: float = 0.0
    day_open: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    target_cr: float = 0.0
    drift: float = 0.0
    buy_p: float = 0.5
    oi_pct: float = 0.0
    noise: float = 0.00011
    mid: float = 0.0          # latent fair price; trades print at the quote around it
    phases: list[tuple[int, int, float, float, float, float]] = field(default_factory=list)  # (from_minute, ...)
    leader: "_Sym | None" = None
    basis: float = 0.0


class SyntheticMarket:
    def __init__(self, universe: Universe, day: date, seed: int = 7, rate: float = 1.0,
                 pattern_every_minutes: float = 4.0, b_range: tuple[float, float] = (0.6, 1.6)):
        self.universe = universe
        self.day = day
        self.rate = rate
        self.rng = random.Random(seed)
        self.pattern_every = pattern_every_minutes
        self.syms: dict[int, _Sym] = {}
        self.injected: list[dict] = []
        self._minute: int | None = None
        for tok in sorted(universe.instruments):
            inst = universe.instruments[tok]
            r = random.Random(seed * 100_003 + tok)
            price = round(r.uniform(80, 2500), 1)
            if inst.kind == Kind.IDX:
                price = 26_000.0 if inst.underlying == "NIFTY" else 58_000.0
            b = r.uniform(*b_range) * (1.6 if inst.kind != Kind.EQ else 1.0)
            tick = inst.tick_size or (0.05 if price >= 100 else 0.01)
            v0 = int(b * 1e7 * 2.5 / price)
            s = _Sym(inst=inst, rng=r, price=price, b_cr=b, tick=tick, v=v0, ct=v0 * price, mid=price,
                     oi=(int(r.uniform(2e6, 2e7)) if inst.is_future else None), prev_close=round(price * r.uniform(0.99, 1.01), 2),
                     day_open=price, day_high=price, day_low=price)
            s.bid, s.ask = price - tick, price + tick
            self.syms[tok] = s
        for s in self.syms.values():
            lt = s.inst.linked_token
            if s.inst.kind == Kind.FUT and lt in self.syms:
                s.leader = self.syms[lt]
                s.basis = s.rng.uniform(0.001, 0.004)
                s.price = round(s.leader.price * (1 + s.basis), 2)
                s.mid = s.price
                s.prev_close = round(s.leader.prev_close * (1 + s.basis), 2)
                s.day_open = s.day_high = s.day_low = s.price
                s.v = int(s.b_cr * 1e7 * 2.5 / s.price)
                s.ct = s.v * s.price
                s.bid, s.ask = s.price - s.tick, s.price + s.tick

    # ------------------------------------------------------------------ baselines
    def baselines(self, sessions: int = 20) -> dict[int, Baseline]:
        out = {}
        for tok, s in self.syms.items():
            prof = {m: s.b_cr * intraday_profile(m) for m in range(555, 940)}
            end = 915 if s.inst.kind == Kind.EQ else 940
            adtv = sum(v for m, v in prof.items() if m < end)
            out[tok] = Baseline(token=tok, profile=prof, adtv_cr=adtv, atr_seed=s.price * 0.0007,
                                sessions=sessions, last_close=s.prev_close, asof=self.day)
        return out

    # ------------------------------------------------------------------ patterns
    def _maybe_inject(self, minute: int) -> None:
        if not (570 <= minute <= 880):
            return
        if self.rng.random() > 1.0 / max(0.1, self.pattern_every):
            return
        candidates = [s for s in self.syms.values()
                      if s.inst.kind == Kind.EQ and s.inst.scannable and not s.phases]
        if not candidates:
            return
        s = self.rng.choice(candidates)
        x = self.rng.random()
        acc = 0.0
        kind = "full"
        for name, w in PATTERN_WEIGHTS:
            acc += w
            if x <= acc:
                kind = name
                break
        start = minute + 1
        phases = []
        m = start
        for dur, mult, drift, buy, oi in PATTERNS[kind]:
            phases.append((m, m + dur, mult, drift, buy, oi))
            m += dur
        s.phases = phases
        self.injected.append({"token": s.inst.token, "symbol": s.inst.display, "kind": kind, "s1_minute": start})
        for f in self.syms.values():
            if f.leader is s:
                f.phases = [(a, b, mult * 0.9, d, buy, oi) for a, b, mult, d, buy, oi in phases]

    def _start_minute(self, s: _Sym, minute: int) -> None:
        prof = intraday_profile(minute)
        mult, drift, buy, oi = 1.0, 0.0, 0.5, 0.0
        noise_sigma = 0.28
        phase = next((p for p in s.phases if p[0] <= minute < p[1]), None)
        if phase is not None:
            _, _, mult, drift, buy, oi = phase
            noise_sigma = 0.07
        elif s.phases and minute >= s.phases[-1][1]:
            s.phases = []
        if phase is None:
            drift = s.rng.gauss(0.0, 0.035)
            buy = min(0.62, max(0.38, s.rng.gauss(0.5, 0.04)))
            oi = s.rng.gauss(0.0, 0.03)
        s.noise = 0.00005 if phase is not None else 0.00011
        s.target_cr = s.b_cr * prof * mult * math.exp(s.rng.gauss(0.0, noise_sigma) - noise_sigma ** 2 / 2)
        s.drift = drift
        s.buy_p = buy
        s.oi_pct = oi

    # ------------------------------------------------------------------ snapshots
    def _round(self, s: _Sym, p: float) -> float:
        return round(round(p / s.tick) * s.tick, 4)

    def _snapshot(self, s: _Sym, ts: float) -> Tick:
        r = s.rng
        per_sec = s.target_cr * 1e7 / 60.0 / max(self.rate, 1e-9)
        rupees = per_sec * r.uniform(0.6, 1.4)
        if s.leader is not None:
            s.mid = s.leader.mid * (1 + s.basis) + r.gauss(0.0, s.mid * 0.00003)
        else:
            s.mid = s.mid * (1 + s.drift / 100.0 / 60.0 / self.rate) + r.gauss(0.0, s.mid * s.noise)
        buy = r.random() < s.buy_p
        # Trade at the standing quote (so the quote rule classifies it), then
        # re-quote around the latent mid: the side mix does not move the price.
        ltp = s.ask if buy else s.bid
        dv = max(1, int(rupees / ltp))
        s.v += dv
        s.ct += dv * ltp
        s.price = ltp
        b = math.floor(s.mid / s.tick) * s.tick
        s.bid, s.ask = round(b, 4), round(b + s.tick, 4)
        s.day_high = max(s.day_high, ltp)
        s.day_low = min(s.day_low, ltp)
        if s.oi is not None:
            s.oi = max(1, int(s.oi * (1 + s.oi_pct / 100.0 / 60.0 / self.rate)))
        q = lambda: int(r.uniform(200, 3000))
        tbq = int(r.uniform(4e4, 9e4) * (1.3 if buy else 0.8))
        tsq = int(r.uniform(4e4, 9e4) * (0.8 if buy else 1.3))
        return Tick(token=s.inst.token, ts=float(int(ts)), ltp=ltp, volume=s.v, atp=round(s.ct / s.v, 2), recv_ts=ts,
                    last_qty=dv, oi=s.oi, bid_px=tuple(round(s.bid - i * s.tick, 4) for i in range(5)),
                    bid_qty=tuple(q() for _ in range(5)), ask_px=tuple(round(s.ask + i * s.tick, 4) for i in range(5)),
                    ask_qty=tuple(q() for _ in range(5)), total_buy_qty=tbq, total_sell_qty=tsq,
                    day_open=s.day_open, day_high=s.day_high, day_low=s.day_low, prev_close=s.prev_close,
                    last_trade_ts=float(int(ts)))

    def second(self, t: float) -> list[Tick]:
        """All snapshots for the exchange second starting at ``t``."""
        minute = minute_of_day(t)
        if minute != self._minute:
            self._minute = minute
            self._maybe_inject(minute)
            for s in self.syms.values():
                self._start_minute(s, minute)
        out: list[Tick] = []
        n_full = int(self.rate)
        frac = self.rate - n_full
        for tok, s in self.syms.items():
            k = n_full + (1 if frac and s.rng.random() < frac else 0)
            for j in range(k):
                out.append(self._snapshot(s, t + 0.05 + j / max(k, 1) * 0.9))
        out.sort(key=lambda x: x.recv_ts)
        return out

    def stream(self, start: float, end: float) -> Iterator[tuple[float, list[Record]]]:
        """(second, records) from ``start`` to ``end`` (exchange time)."""
        t = float(int(start))
        # one pre-open snapshot per instrument so 09:15 is a complete candle
        pre = at(self.day, "09:14", 50)
        if t <= pre:
            yield pre, [self._snapshot(s, pre) for s in self.syms.values()]
            t = max(t, at(self.day, "09:15"))
        while t < end:
            yield t, self.second(t)
            t += 1.0


class SimulatedFeed(MarketDataAdapter):
    """Plays (second, records) pairs at ``speed`` x real time and drives the
    clock (``on_time``) like the live timer would. Used by the demo and by
    replays with the dashboard."""

    name = "simulated"
    drives_clock = True

    def __init__(self, source: Iterable[tuple[float, list[Record]]], speed: float = 1.0,
                 fast_until: float | None = None, frames_per_second: int = 10, recorded_clock: bool = False):
        super().__init__()
        self.recorded_clock = recorded_clock
        self.source = source
        self.speed = speed
        self.fast_until = fast_until
        self.frames_per_second = max(1, frames_per_second)
        self.on_time: Callable[[float], None] | None = None
        self._stop = asyncio.Event()
        self.sim_time: float | None = None
        self.done = False

    async def close(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        self.connected = True
        self.emit_status("connected")
        wall0: float | None = None
        sim0: float | None = None
        n = 0
        for t, records in self.source:
            if self._stop.is_set():
                break
            if self.fast_until is not None and t < self.fast_until:
                n += 1
                if n % 30 == 0:
                    await asyncio.sleep(0)
            elif self.speed > 0:
                if wall0 is None:
                    wall0, sim0 = time.perf_counter(), t
                delay = wall0 + (t - sim0) / self.speed - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
            else:
                n += 1
                if n % 30 == 0:
                    await asyncio.sleep(0)
            self.sim_time = t
            if self.on_time is not None and not self.recorded_clock:
                self.on_time(t)
            realtime = self.speed > 0 and wall0 is not None
            frames = self.frames_per_second if realtime else 1
            per = max(1, -(-len(records) // frames))
            for k in range(0, len(records), per):
                if realtime and k:
                    delay = wall0 + (t + k / max(1, len(records)) - sim0) / self.speed - time.perf_counter()
                    if delay > 0:
                        await asyncio.sleep(delay)
                ticks: list[Tick] = []
                for r in records[k:k + per]:
                    if isinstance(r, FeedEvent):
                        if ticks:
                            self.emit_ticks(ticks)
                            ticks = []
                        if r.kind == "clock":
                            if self.on_time is not None:
                                self.on_time(r.ts)
                        else:
                            self.emit_status("disconnected" if r.kind == "disconnect" else "reconnected", r.ts)
                    else:
                        ticks.append(r)
                if ticks:
                    self.emit_ticks(ticks)
        self.done = True
        self.connected = False


def group_by_second(records: Iterable[Record]) -> Iterator[tuple[float, list[Record]]]:
    """Recorded data -> (second, records) pairs for SimulatedFeed."""
    cur: float | None = None
    bucket: list[Record] = []
    for r in records:
        t = float(int(r.recv_ts or r.ts))
        if cur is None:
            cur = t
        if t != cur:
            yield cur, bucket
            bucket = []
            cur = t
        bucket.append(r)
    if cur is not None:
        yield cur, bucket


def synthetic_universe(day: date, n_stocks: int = 40, seed: int = 7, with_futures: bool = True) -> Universe:
    """A made-up F&O universe (tokens and symbols are fictional)."""
    rng = random.Random(seed)
    names = [f"STK{i:03d}" for i in range(1, n_stocks + 1)]
    instruments: dict[int, Instrument] = {}
    underlyings: dict[str, dict[str, int]] = {}
    for i, und in enumerate(names):
        eq = Instrument(token=100_000 + i * 2, symbol=und, underlying=und, kind=Kind.EQ, tick_size=0.05)
        instruments[eq.token] = eq
        entry = {"EQ": eq.token}
        if with_futures:
            fu = Instrument(token=100_001 + i * 2, symbol=f"{und}FUT", underlying=und, kind=Kind.FUT,
                            exchange="NFO", lot_size=rng.choice([250, 500, 700, 1000, 1500, 3000]),
                            tick_size=0.05, series="current")
            eq.linked_token, fu.linked_token = fu.token, eq.token
            instruments[fu.token] = fu
            entry["FUT"] = fu.token
        underlyings[und] = entry
    regime = {}
    for j, name in enumerate(("NIFTY", "BANKNIFTY")):
        ix = Instrument(token=900_000 + j, symbol=f"{name}FUT", underlying=name, kind=Kind.IDX, exchange="NFO",
                        lot_size=75, tick_size=0.1, series="current", scannable=False, scan_block_reason="regime_only")
        instruments[ix.token] = ix
        regime[name] = ix.token
    return Universe(date=day, instruments=instruments, underlyings=underlyings, regime=regime,
                    notes=["synthetic universe: fictional symbols and prices"])
