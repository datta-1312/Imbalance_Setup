"""1-minute candle builder on exchange time (spec §3, §8.3).

* Turnover comes from the cumulative fields: candle turnover is the change in
  ATP x day-volume across the candle's boundaries, so missed snapshots never
  lose turnover. Implausible values fall back to ΔV x mid and are flagged.
* All instruments' candles for a minute close together ("batch close") at the
  boundary plus the grace period. Snapshots of the next minute that arrive inside
  the grace window start the next candle; snapshots stamped in an already-closed
  minute are attributed to the open candle (cumulative fields keep the totals right).
* The first snapshot of the day only sets the cumulative baseline, so the
  pre-open auction trade never inflates the 09:15 candle.
* Candles overlapping a feed disconnect or a stale period are flagged GAP and
  cannot trigger a surge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .calendar import SessionRules
from .flow import UNKNOWN, classify
from .models import Candle, Flag, Instrument, Kind, Tick
from .timeutil import minute_of_day, minute_start


@dataclass(slots=True)
class ClosedBatch:
    minute_start: float
    candles: dict[int, Candle]


@dataclass(slots=True)
class StatusChange:
    token: int
    ts: float
    status: str  # "stale" | "fresh"


@dataclass(slots=True)
class _St:
    inst: Instrument
    seen: bool = False
    ct: float = 0.0          # cumulative turnover (₹) at the last snapshot
    v: int = 0               # cumulative volume (units) at the last snapshot
    ltp: float = 0.0
    atp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    side: int = UNKNOWN
    oi: int | None = None
    last_ts: float = 0.0
    last_recv: float = 0.0
    prev_close: float | None = None
    forming: Candle | None = None
    nxt: Candle | None = None
    pending_gap: bool = False
    stale: bool = False


class CandleBuilder:
    def __init__(self, instruments: Mapping[int, Instrument], session: SessionRules,
                 stale_after_s: float = 30.0, futures_volume_in_lots: bool = False,
                 plausibility_tol: float = 0.01):
        self.session = session
        self.grace = session.grace
        self.stale_after = stale_after_s
        self.lots = futures_volume_in_lots
        self.tol = plausibility_tol
        self._st: dict[int, _St] = {}
        self._order: list[int] = []
        for inst in instruments.values():
            self.add_instrument(inst)
        self.open_minute: float | None = None
        self.feed_down = False
        self.counters = {"ticks": 0, "late": 0, "anomalies": 0, "unknown_token": 0, "fallback": 0, "gap": 0}
        self._status: list[StatusChange] = []

    # ------------------------------------------------------------------ setup
    def add_instrument(self, inst: Instrument) -> None:
        if inst.token in self._st:
            self._st[inst.token].inst = inst
            return
        self._st[inst.token] = _St(inst=inst)
        self._order = sorted(self._st)

    def next_deadline(self) -> float | None:
        if self.open_minute is None:
            return None
        return self.open_minute + 60.0 + self.grace

    def forming(self, token: int) -> Candle | None:
        st = self._st.get(token)
        return st.forming if st else None

    def last_price(self, token: int) -> float | None:
        st = self._st.get(token)
        return st.ltp if st and st.seen else None

    def is_stale(self, token: int) -> bool:
        st = self._st.get(token)
        return bool(st and st.stale)

    def stale_tokens(self) -> list[int]:
        return [t for t in self._order if self._st[t].stale]

    def drain_status(self) -> list[StatusChange]:
        out, self._status = self._status, []
        return out

    # ------------------------------------------------------------------ feed status
    def on_disconnect(self, ts: float) -> None:
        self.feed_down = True
        for st in self._st.values():
            for c in (st.forming, st.nxt):
                if c is not None:
                    c.flags |= Flag.GAP
            st.pending_gap = True

    def on_reconnect(self, ts: float) -> None:
        self.feed_down = False

    # ------------------------------------------------------------------ ticks
    def on_tick(self, t: Tick) -> tuple[Candle | None, list[ClosedBatch]]:
        st = self._st.get(t.token)
        if st is None:
            self.counters["unknown_token"] += 1
            return None, []
        self.counters["ticks"] += 1
        inst = st.inst
        vol = t.volume * inst.lot_size if (self.lots and inst.is_future) else t.volume
        ct = t.atp * vol
        m = minute_start(t.ts)
        in_sess = self.session.candle_allowed(inst.kind, minute_of_day(m))
        batches: list[ClosedBatch] = []
        if in_sess:
            if self.open_minute is None:
                self.open_minute = m
            else:
                while m > self.open_minute + 60.0:
                    b = self._batch_close()
                    if b.candles:
                        batches.append(b)

        if st.stale:
            st.stale = False
            st.pending_gap = True
            self._status.append(StatusChange(t.token, t.recv_ts or t.ts, "fresh"))
        st.last_ts = t.ts
        st.last_recv = t.recv_ts or t.ts
        if t.prev_close:
            st.prev_close = t.prev_close

        if not st.seen:
            st.seen = True
            st.ct, st.v = ct, vol
            st.ltp, st.atp = t.ltp, t.atp
            self._quote(st, t)
            if t.oi is not None:
                st.oi = t.oi
            if not in_sess:
                return None, batches
            c = self._candle_for(st, m)
            c.flags |= Flag.PARTIAL
            self._apply(c, st, t)
            c.ct_start = c.ct_end = ct
            c.v_start = c.v_end = vol
            return c, batches

        dv = vol - st.v
        side = UNKNOWN
        mid = t.ltp
        if dv > 0:
            side = classify(t.ltp, st.ltp, st.bid, st.ask, st.side)
            if st.bid > 0 and st.ask > 0:
                mid = (st.bid + st.ask) / 2.0

        if not in_sess:
            # Pre-open, closing auction or after the close: keep the cumulative
            # baseline current but build no candle (auction volume is excluded).
            self._commit(st, t, ct, vol, side)
            return None, batches

        c = self._candle_for(st, m)
        if dv > 0:
            if side > 0:
                c.buy_vol += dv
            elif side < 0:
                c.sell_vol += dv
            else:
                c.unk_vol += dv
            c.fallback_rupees += dv * mid
        elif dv < 0:
            self._anomaly(c, -dv, t.ltp)
        c.ct_end = ct
        c.v_end = vol
        self._apply(c, st, t)
        if st.pending_gap:
            c.flags |= Flag.GAP
            st.pending_gap = False
        if self.feed_down:
            c.flags |= Flag.GAP
        self._commit(st, t, ct, vol, side)
        return c, batches

    def _commit(self, st: _St, t: Tick, ct: float, vol: int, side: int) -> None:
        st.ct, st.v = ct, vol
        st.ltp = t.ltp
        if t.atp > 0:
            st.atp = t.atp
        if side != UNKNOWN:
            st.side = side
        self._quote(st, t)
        if t.oi is not None:
            st.oi = t.oi

    @staticmethod
    def _quote(st: _St, t: Tick) -> None:
        st.bid = t.bid_px[0] if t.bid_px else 0.0
        st.ask = t.ask_px[0] if t.ask_px else 0.0

    def _anomaly(self, c: Candle, removed: int, ltp: float) -> None:
        """Cumulative volume went backwards (feed correction): accept the new
        cumulative values and shrink the classified volumes proportionally."""
        self.counters["anomalies"] += 1
        c.flags |= Flag.ANOMALY
        tot = c.classified_vol
        if tot > 0:
            keep = max(0.0, (tot - removed) / tot)
            c.buy_vol = int(c.buy_vol * keep)
            c.sell_vol = int(c.sell_vol * keep)
            c.unk_vol = int(c.unk_vol * keep)
        c.fallback_rupees = max(0.0, c.fallback_rupees - removed * ltp)

    def _new_candle(self, st: _St, mstart: float) -> Candle:
        p = st.ltp
        c = Candle(token=st.inst.token, start=mstart, open=p, high=p, low=p, close=p,
                   ct_start=st.ct, ct_end=st.ct, v_start=st.v, v_end=st.v, ref_price=p,
                   oi_open=st.oi, oi_close=st.oi, vwap=(st.atp or None), prev_day_close=st.prev_close)
        if self.session.in_block_window(minute_of_day(mstart)):
            c.flags |= Flag.BLOCK_WINDOW
        if self.feed_down or st.stale:
            c.flags |= Flag.GAP
        return c

    def _candle_for(self, st: _St, m: float) -> Candle:
        om = self.open_minute
        assert om is not None
        if m <= om:
            if st.nxt is not None:
                # Cumulative fields must be attributed in arrival order: once the
                # next minute has started for this instrument, everything goes there.
                self.counters["late"] += 1
                st.nxt.flags |= Flag.LATE
                return st.nxt
            if st.forming is None:
                st.forming = self._new_candle(st, om)
            if m < om:
                self.counters["late"] += 1
                st.forming.flags |= Flag.LATE
            return st.forming
        # m == om + 60: a snapshot of the next minute inside the grace window.
        if st.forming is None and self.session.candle_allowed(st.inst.kind, minute_of_day(om)):
            st.forming = self._new_candle(st, om)   # placeholder: no snapshot this minute
        if st.nxt is None:
            st.nxt = self._new_candle(st, m)
        return st.nxt

    @staticmethod
    def _apply(c: Candle, st: _St, t: Tick) -> None:
        p = t.ltp
        if c.n_ticks == 0:
            c.open = c.high = c.low = c.close = p
        else:
            if p > c.high:
                c.high = p
            if p < c.low:
                c.low = p
            c.close = p
        c.n_ticks += 1
        if t.atp > 0:
            c.vwap = t.atp
        if t.oi is not None:
            if c.oi_open is None:
                c.oi_open = t.oi
            c.oi_close = t.oi
        c.total_buy_qty = t.total_buy_qty
        c.total_sell_qty = t.total_sell_qty
        if t.bid_qty:
            c.bid5_qty = sum(t.bid_qty[:5])
        if t.ask_qty:
            c.ask5_qty = sum(t.ask_qty[:5])
        if t.prev_close:
            c.prev_day_close = t.prev_close

    # ------------------------------------------------------------------ clock
    def advance(self, now: float) -> list[ClosedBatch]:
        """Close every minute whose deadline (boundary + grace) is <= ``now``."""
        out: list[ClosedBatch] = []
        if self.open_minute is None:
            m = minute_start(now)
            if any(self.session.candle_allowed(k, minute_of_day(m)) for k in _KINDS):
                self.open_minute = m
            return out
        while now >= self.open_minute + 60.0 + self.grace:
            b = self._batch_close()
            if b.candles:
                out.append(b)
        return out

    def check_stale(self, now: float) -> None:
        mod = minute_of_day(now)
        # Silence before continuous trading (pre-open buffer) is not staleness.
        session_start = minute_start(now) - (mod - self.session.cont_start) * 60.0
        for tok in self._order:
            st = self._st[tok]
            if not st.seen or st.stale:
                continue
            if not self.session.candle_allowed(st.inst.kind, mod):
                continue
            if now - max(st.last_recv, session_start) >= self.stale_after:
                st.stale = True
                if st.forming is not None:
                    st.forming.flags |= Flag.GAP
                self._status.append(StatusChange(tok, now, "stale"))

    def _batch_close(self) -> ClosedBatch:
        om = self.open_minute
        assert om is not None
        mod = minute_of_day(om)
        closed: dict[int, Candle] = {}
        for tok in self._order:
            st = self._st[tok]
            c = st.forming
            if c is None:
                if st.seen and self.session.candle_allowed(st.inst.kind, mod):
                    c = self._new_candle(st, om)
                else:
                    st.forming, st.nxt = st.nxt, None
                    continue
            if self.feed_down:
                c.flags |= Flag.GAP
            if c.n_ticks == 0:
                c.flags |= Flag.NO_DATA
            self._finalize(c)
            closed[tok] = c
            st.forming, st.nxt = st.nxt, None
        self.open_minute = om + 60.0
        return ClosedBatch(om, closed)

    def _finalize(self, c: Candle) -> None:
        c.volume = max(0, c.v_end - c.v_start)
        raw = c.ct_end - c.ct_start
        if c.volume == 0:
            c.turnover_cr = 0.0
        else:
            implied = raw / c.volume
            err = 0.005 * (c.v_end + c.v_start) / c.volume   # ATP is rounded to paise
            ref = c.ref_price if c.ref_price > 0 else c.low
            lo = min(c.low, ref) * (1 - self.tol) - err
            hi = max(c.high, ref) * (1 + self.tol) + err
            if raw < 0 or implied < lo or implied > hi:
                c.turnover_cr = c.fallback_rupees / 1e7
                c.flags |= Flag.FALLBACK
                self.counters["fallback"] += 1
            else:
                c.turnover_cr = raw / 1e7
        if c.flags & Flag.GAP:
            self.counters["gap"] += 1
        c.closed = True


_KINDS = (Kind.EQ, Kind.FUT)
