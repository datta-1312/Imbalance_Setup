"""Scripted snapshot streams for tests, demos and acceptance scenarios.

``TickScript`` emits broker-like snapshots (cumulative volume, paise-rounded ATP,
5-level quotes, OI) that the candle builder turns into candles with the chosen
OHLC, turnover, estimated buy share and OI change. Quotes are set so that the
quote rule classifies each volume increment as the requested side.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date

from .models import Tick
from .timeutil import at


@dataclass(slots=True)
class FeedEvent:
    kind: str          # "disconnect" | "reconnect"
    ts: float
    recv_ts: float


Record = Tick | FeedEvent


def record_time(r: Record) -> float:
    return r.recv_ts if r.recv_ts else r.ts


def _path(o: float, h: float, l: float, c: float, n: int) -> list[float]:
    """Piecewise-linear price path through the candle's extremes.

    Green: O -> L -> H -> C; red/flat: O -> H -> L -> C."""
    if n == 1:
        return [c]
    pts = [o, l, h, c] if c > o else [o, h, l, c]
    idx = [0, round((n - 1) / 3), round(2 * (n - 1) / 3), n - 1]
    out = []
    for i in range(n):
        for s in range(3):
            a, b = idx[s], idx[s + 1]
            if a <= i <= b:
                if b == a:
                    out.append(pts[s + 1])
                else:
                    f = (i - a) / (b - a)
                    out.append(pts[s] + (pts[s + 1] - pts[s]) * f)
                break
    return out


class TickScript:
    def __init__(self, token: int, day: date, price: float, tick_size: float = 0.05,
                 oi: int | None = None, prev_close: float | None = None, per_minute: int = 30,
                 recv_delay: float = 0.25, opening_volume: int = 100_000, depth_qty: int = 500):
        self.token = token
        self.day = day
        self.tick = tick_size
        self.ltp = price
        self.oi = oi
        self.prev_close = prev_close if prev_close is not None else price
        self.per_minute = per_minute
        self.recv_delay = recv_delay
        self.depth_qty = depth_qty
        self.v = opening_volume                  # cumulative volume incl. opening auction
        self.ct = opening_volume * price         # exact cumulative turnover (₹)
        self.records: list[Record] = []
        self.day_open = price
        self.day_high = price
        self.day_low = price

    # ------------------------------------------------------------------ helpers
    def _round(self, p: float) -> float:
        return round(round(p / self.tick) * self.tick, 6)

    def _quote(self, t: Tick, bid: float, ask: float) -> None:
        q = self.depth_qty
        t.bid_px = tuple(round(bid - i * self.tick, 6) for i in range(5))
        t.ask_px = tuple(round(ask + i * self.tick, 6) for i in range(5))
        t.bid_qty = (q,) * 5
        t.ask_qty = (q,) * 5

    def _last_tick(self) -> Tick | None:
        for r in reversed(self.records):
            if isinstance(r, Tick):
                return r
        return None

    def _emit(self, ts: float, ltp: float, dv: int, side: int, atp_factor: float = 1.0,
              tbq: int = 50_000, tsq: int = 50_000, oi: int | None = None) -> Tick:
        prev = self._last_tick()
        if prev is not None and dv > 0:
            # Make the quote rule classify this increment as `side`.
            if side > 0:
                self._quote(prev, ltp - self.tick, ltp)
            elif side < 0:
                self._quote(prev, ltp, ltp + self.tick)
        self.v += dv
        self.ct += dv * ltp
        self.ltp = ltp
        self.day_high = max(self.day_high, ltp)
        self.day_low = min(self.day_low, ltp)
        atp = round(self.ct / self.v, 2) if self.v else ltp
        t = Tick(token=self.token, ts=ts, ltp=ltp, volume=self.v, atp=round(atp * atp_factor, 2),
                 recv_ts=ts + self.recv_delay, last_qty=max(1, dv) if dv else 0,
                 oi=oi if oi is not None else self.oi, total_buy_qty=tbq, total_sell_qty=tsq,
                 day_open=self.day_open, day_high=self.day_high, day_low=self.day_low,
                 prev_close=self.prev_close, last_trade_ts=ts)
        self._quote(t, ltp - self.tick, ltp + self.tick)
        self.records.append(t)
        return t

    # ------------------------------------------------------------------ public
    def preopen(self, hhmm: str = "09:14", second: int = 30) -> "TickScript":
        """One pre-open snapshot carrying the opening-auction volume."""
        self._emit(at(self.day, hhmm, second), self.ltp, 0, 0)
        return self

    def feed_event(self, kind: str, hhmm: str, second: float) -> "TickScript":
        ts = at(self.day, hhmm, second)
        self.records.append(FeedEvent(kind, ts, ts))
        return self

    def candle(self, hhmm: str, o: float, h: float, l: float, c: float, turnover_cr: float,
               buy_share: float = 0.5, oi_change_pct: float = 0.0, n: int | None = None,
               skip: tuple[float, float] | None = None, weights: list[float] | None = None,
               atp_glitch: dict[int, float] | None = None, book: tuple[int, int] | None = None) -> "TickScript":
        """Snapshots for one 1-minute candle starting at ``hhmm``.

        ``skip=(s0, s1)``: no snapshots with exchange second in [s0, s1) (their
        trades show up in the next snapshot's cumulative fields).
        ``weights``: relative rupee turnover per snapshot (default equal).
        ``atp_glitch``: {snapshot index: factor} reports a corrupted ATP.
        """
        n = n or self.per_minute
        base = at(self.day, hhmm)
        prices = [self._round(p) for p in _path(o, h, l, c, n)]
        prices[0], prices[-1] = self._round(o), self._round(c)
        w = weights or [1.0] * n
        wsum = sum(w)
        total = turnover_cr * 1e7
        oi0 = self.oi
        step = 60.0 / n
        buy_so_far = 0.0
        vol_so_far = 0.0
        tbq, tsq = book or ((60_000, 40_000) if buy_share >= 0.5 else (40_000, 60_000))
        for i in range(n):
            sec = math.floor(i * step)
            p = prices[i]
            dv = max(1, int(round(total * w[i] / wsum / p))) if total > 0 else 0
            # side assignment keeps the running buy share close to the target
            if dv:
                side = 1 if (buy_so_far + dv / 2) <= buy_share * (vol_so_far + dv) else -1
                if side > 0:
                    buy_so_far += dv
                vol_so_far += dv
            else:
                side = 0
            oi = None
            if oi0 is not None:
                oi = int(round(oi0 * (1 + oi_change_pct / 100.0 * (i + 1) / n)))
            if skip is not None and skip[0] <= sec < skip[1]:
                # Trades happen but no snapshot is delivered: the next snapshot's
                # cumulative fields carry them.
                self.v += dv
                self.ct += dv * p
                self.ltp = p
                continue
            factor = atp_glitch.get(i, 1.0) if atp_glitch else 1.0
            self._emit(base + sec, p, dv, side, factor, tbq, tsq, oi)
        if self.oi is not None and oi0 is not None:
            self.oi = int(round(oi0 * (1 + oi_change_pct / 100.0)))
        return self

    def quiet(self, hhmm: str, count: int, turnover_cr: float = 0.5, drift_pct: float = 0.0,
              range_pct: float = 0.1, buy_share: float = 0.5, oi_change_pct: float = 0.0) -> "TickScript":
        """``count`` small candles starting at ``hhmm`` drifting by drift_pct each."""
        from .timeutil import fmt_minute, parse_hhmm

        m0 = parse_hhmm(hhmm)
        for k in range(count):
            o = self.ltp
            c = self._round(o * (1 + drift_pct / 100.0))
            half = max(self.tick, o * range_pct / 100.0 / 2)
            h = self._round(max(o, c) + half)
            l = self._round(min(o, c) - half)
            self.candle(fmt_minute(m0 + k), o, h, l, c, turnover_cr, buy_share=buy_share,
                        oi_change_pct=oi_change_pct)
        return self


def merge_records(*scripts: TickScript | list[Record]) -> list[Record]:
    """Merge several scripts' records in receive-time order (stable)."""
    allrec: list[tuple[float, int, int, Record]] = []
    for si, s in enumerate(scripts):
        recs = s.records if isinstance(s, TickScript) else s
        for ri, r in enumerate(recs):
            allrec.append((record_time(r), si, ri, r))
    allrec.sort(key=lambda x: (x[0], x[1], x[2]))
    return [r for _, _, _, r in allrec]
