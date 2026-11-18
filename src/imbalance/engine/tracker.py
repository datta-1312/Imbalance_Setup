"""Per-instrument state machine (spec §5):

    IDLE -> SURGE_1 -> PULLBACK -> ARMED -> ENTRY -> (trade) -> COMPLETED
    any live state -> INVALIDATED | EXPIRED, then a cooldown before IDLE.

Candles are evaluated in the setup's "view" space: the long setup uses real
prices; the optional short setup uses negated prices with buy and sell volume
swapped (see ``models.mirror``), so both directions share this code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..config import Params
from ..indicators import WilderATR
from ..models import UNTRADEABLE, Candle, Flag, Instrument, Kind, mirror, view
from ..timeutil import fmt_hm, fmt_minute, minute_of_day
from . import rules
from .risk import compute_plan, round_up
from .scorer import (OI_PULLBACK_SCORE, OI_SURGE_SCORE, book_component, buy_share_component, compute_score,
                     ramp, regime_component, rvol_component)
from .types import LIVE_STATES, AlertType, Check, Evaluation, Event, Reason, State, evaluation

if TYPE_CHECKING:
    from .engine import Ctx, ScreenerEngine

_FLAG_IDX = {("s1", 1): 0, ("s2", 1): 1, ("s1", -1): 2, ("s2", -1): 3}
_NO_FLAGS = (False, False, False, False)


@dataclass
class Setup:
    id: str
    token: int
    symbol: str
    display: str
    kind: str
    direction: int
    state: State
    s1_start: float
    s1_confirmed_ts: float
    s1_open: float
    s1_high: float
    s1_low: float
    s1_close: float
    s1_turnover: float
    s1_turnover_first: float
    s1_rvol: float | None
    s1_buy_share: float
    s1_move_pct: float | None
    s1_window: int
    leg_start: float
    H: float
    h_start: float
    floor: float
    expire_at: float
    low_since_h: float | None = None
    L: float | None = None
    P: float | None = None
    retracement: float = 0.0
    pb_start: float | None = None
    pb_candles: int = 0
    pb_turnover_sum: float = 0.0
    pb_turnover_n: int = 0
    pb_vol: int = 0
    pb_sell: int = 0
    oi_at_h: int | None = None
    armed_ts: float | None = None
    entry_ts: float | None = None
    end_ts: float | None = None
    end_reason: str | None = None
    extended: bool = False
    tags: list[str] = field(default_factory=list)
    oi: dict[str, Any] = field(default_factory=dict)
    cash_fut: dict[str, bool] = field(default_factory=dict)
    grades: dict[str, dict] = field(default_factory=dict)
    checks: dict[str, list[dict]] = field(default_factory=dict)
    s1_metrics: dict[str, Any] = field(default_factory=dict)
    s2: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    trade_id: str | None = None
    suppressed: str | None = None
    history: list[dict] = field(default_factory=list)
    last_close: float | None = None

    # -- helpers -------------------------------------------------------------
    def real(self, x: float | None) -> float | None:
        return None if x is None else round(self.direction * x, 6)

    def real_ohlc(self, o: float, h: float, l: float, c: float) -> dict[str, float | None]:
        """View-space OHLC back to real prices (a short's view high is the real low)."""
        rh, rl = (h, l) if self.direction > 0 else (-l, -h)
        return {"open": self.real(o), "high": round(rh, 6), "low": round(rl, 6), "close": self.real(c)}

    @property
    def pb_ratio(self) -> float:
        if self.pb_turnover_n == 0 or self.s1_turnover <= 0:
            return 0.0
        return self.pb_turnover_sum / self.pb_turnover_n / self.s1_turnover

    @property
    def pb_sell_share(self) -> float | None:
        return self.pb_sell / self.pb_vol if self.pb_vol else None

    def add_tag(self, tag: str) -> None:
        if tag not in self.tags:
            self.tags.append(tag)

    @property
    def grade(self) -> dict | None:
        for stage in ("entry", "armed", "s1"):
            if stage in self.grades:
                return self.grades[stage]
        return None

    def core_values(self) -> dict[str, Any]:
        """Values logged with every transition (real prices)."""
        return {
            "direction": self.direction, "s1_time": fmt_hm(self.s1_start), "s1_turnover_cr": self.s1_turnover,
            "s1_rvol": self.s1_rvol, "H": self.real(self.H), "leg_start": self.real(self.leg_start),
            "L": self.real(self.L), "P": self.real(self.P), "floor": self.real(self.floor),
            "retracement": self.retracement, "pullback_candles": self.pb_candles,
            "pullback_turnover_ratio": self.pb_ratio, "pullback_sell_share": self.pb_sell_share,
        }

    def row(self, now: float | None = None) -> dict[str, Any]:
        g = self.grade or {}
        p_dist = None
        if self.P is not None and self.last_close:
            p_dist = (self.P - self.last_close) / abs(self.last_close) * 100.0
        return {
            "id": self.id, "token": self.token, "symbol": self.symbol, "display": self.display,
            "kind": self.kind, "direction": self.direction, "state": self.state.value,
            "s1_start": self.s1_start, "s1_time": fmt_hm(self.s1_start), "s1_confirmed_ts": self.s1_confirmed_ts,
            "s1_turnover": self.s1_turnover, "s1_rvol": self.s1_rvol, "s1_move_pct": self.s1_move_pct,
            "s1_buy_share": self.s1_buy_share,
            "s1_ohlc": self.real_ohlc(self.s1_open, self.s1_high, self.s1_low, self.s1_close),
            "H": self.real(self.H), "L": self.real(self.L), "P": self.real(self.P), "floor": self.real(self.floor),
            "leg_start": self.real(self.leg_start), "retracement": self.retracement,
            "pb_ratio": self.pb_ratio, "pb_candles": self.pb_candles, "pb_sell_share": self.pb_sell_share,
            "distance_to_trigger_pct": p_dist, "last": self.real(self.last_close),
            "oi": self.oi, "cash_fut": self.cash_fut, "grade": g.get("grade"), "score": g.get("score"),
            "grades": self.grades, "tags": list(self.tags), "expire_at": self.expire_at,
            "minutes_since_s1": ((now - self.s1_start) / 60.0) if now else None,
            "minutes_left": ((self.expire_at - now) / 60.0) if now else None,
            "armed_ts": self.armed_ts, "entry_ts": self.entry_ts, "end_ts": self.end_ts,
            "end_reason": self.end_reason, "extended": self.extended, "plan": self.plan, "s2": self.s2,
            "trade_id": self.trade_id, "suppressed": self.suppressed, "checks": self.checks,
            "history": self.history,
        }


class Tracker:
    """One instrument: candle history, indicators, and at most one live setup."""

    def __init__(self, inst: Instrument, eng: "ScreenerEngine"):
        self.inst = inst
        self.eng = eng
        self.candles: list[Candle] = []
        self._mirrored: list[Candle] = []
        b = eng.baselines.get(inst.token)
        self.atr = WilderATR(self.params.atr_period, b.atr_seed if b else None)
        self.stale = False
        self.paused = False
        self.setup: Setup | None = None
        self.cooldown_until = 0.0
        self.prov: dict[str, tuple[float, int]] = {}         # "S1"/"S2" -> (candle start, direction)
        self.flags: dict[float, tuple[bool, bool, bool, bool]] = {}
        self.prev_day_close: float | None = b.last_close if b else None
        self.last_price: float | None = None

    # ------------------------------------------------------------------ context
    @property
    def params(self) -> Params:
        return self.eng.params_for(self.inst)

    @property
    def baseline(self):
        return self.eng.baselines.get(self.inst.token)

    def hist(self, d: int) -> list[Candle]:
        if d > 0:
            return self.candles
        while len(self._mirrored) < len(self.candles):
            self._mirrored.append(mirror(self.candles[len(self._mirrored)]))
        return self._mirrored

    def b_sum(self, cs: list[Candle], p: Params) -> float | None:
        b = self.baseline
        if b is None or b.sessions < p.baseline_min_sessions:
            return None
        total = 0.0
        for c in cs:
            v = b.b(c.minute)
            if v is None:
                return None
            total += v
        return total

    def _event(self, kind: str, ts: float, s: Setup | None, **kw: Any) -> Event:
        d = kw.pop("direction", None)
        if d is None:
            d = s.direction if s else 1
        return Event(kind=kind, ts=ts, token=self.inst.token, symbol=self.inst.symbol, display=self.inst.display,
                     setup_id=s.id if s else None, direction=d, **kw)

    def _transition(self, s: Setup, ts: float, frm: State, to: State, reason: str, alert: str | None,
                    values: dict[str, Any], grade: dict | None = None) -> Event:
        s.history.append({"ts": ts, "from": frm.value, "to": to.value, "reason": reason})
        vals = {**s.core_values(), **values}
        return self._event("transition", ts, s, from_state=frm.value, to_state=to.value, reason=reason,
                           alert=alert, grade=(grade or {}).get("grade"), score=(grade or {}).get("score"),
                           values=vals)

    # ------------------------------------------------------------------ commits
    def commit(self, c: Candle) -> None:
        """Append a closed candle to history and update indicators."""
        self.candles.append(c)
        self.atr.update(c.high, c.low, c.close)
        self.last_price = c.close
        if c.prev_day_close:
            self.prev_day_close = c.prev_day_close
        if self.setup is not None:
            self.setup.last_close = c.close * self.setup.direction

    def compute_flags(self, c: Candle) -> None:
        """Volume-surge flags used by the linked instrument's cash–futures check."""
        p = self.params
        if not c.tradeable:
            f = _NO_FLAGS
        else:
            bm = self.b_sum([c], p)
            rv = c.turnover_cr / bm if bm else 0.0
            s1 = c.turnover_cr >= p.surge_turnover_floor_cr and rv >= p.surge_rvol_min
            s2 = c.turnover_cr >= p.s2_floor_cr and rv >= p.s2_rvol_min
            f = (s1 and c.green, s2 and c.green, s1 and c.red, s2 and c.red)
        self.flags[c.start] = f
        if len(self.flags) > 6:
            for k in sorted(self.flags)[:-6]:
                del self.flags[k]

    def linked_confirms(self, stage: str, direction: int, minutes: list[float]) -> bool:
        lt = self.inst.linked_token
        other = self.eng.trackers.get(lt) if lt is not None else None
        if other is None:
            return False
        idx = _FLAG_IDX[(stage, 1 if direction > 0 else -1)]
        return any(other.flags.get(m, _NO_FLAGS)[idx] for m in minutes)

    # ------------------------------------------------------------------ eligibility
    def near_band(self, price: float, p: Params) -> tuple[bool, float | None]:
        lo, hi = self.inst.lower_band, self.inst.upper_band
        if price <= 0 or (not lo and not hi):
            return False, None
        dists = []
        if hi:
            dists.append((hi - price) / price * 100.0)
        if lo:
            dists.append((price - lo) / price * 100.0)
        d = min(dists)
        return d <= p.price_band_proximity_pct, d

    def circuit_hit(self, c: Candle) -> bool:
        lo, hi = self.inst.lower_band, self.inst.upper_band
        return bool((hi and c.high >= hi) or (lo and c.low <= lo))

    def _common_checks(self, cs: list[Candle], last: Candle, p: Params, ctx: "Ctx") -> list[Check]:
        flags = Flag.NONE
        for x in cs:
            flags |= x.flags
        near, dist = self.near_band(last.close, p)
        halted = ctx.day.events.is_halted(ctx.day.date, self.inst.underlying)
        return [
            Check("data_quality", _flag_str(flags & (UNTRADEABLE | Flag.ANOMALY)) or ("stale" if self.stale else "ok"),
                  "ok", not (flags & UNTRADEABLE) and not self.stale),
            Check("price_band", dist, p.price_band_proximity_pct, not near),
            Check("not_halted", halted, False, not halted),
        ]

    def _s1_eligibility(self, cs: list[Candle], last: Candle, direction: int, p: Params, ctx: "Ctx",
                        continuation: bool = False) -> list[Check]:
        first = cs[0]
        checks = [
            Check("scannable", self.inst.scan_block_reason or ("paused" if self.paused else "ok"), "ok",
                  self.inst.scannable and not self.paused),
            Check("session_window", fmt_minute(first.minute), f"{p.no_signal_before}-{p.last_surge1_time}",
                  ctx.session.surge1_allowed(first.minute, p)),
        ]
        if not continuation:
            checks.append(Check("cooldown", fmt_hm(self.cooldown_until) if self.cooldown_until else "none", "elapsed",
                                first.start >= self.cooldown_until))
        checks += self._common_checks(cs, last, p, ctx)
        if p.event_day_mode == "suppress" and ctx.day.events.is_event_day(ctx.day.date, self.inst.underlying):
            checks.append(Check("event_day", True, "suppressed", False))
        if self._needs_cash_confirmation(p):
            minutes = sorted({x.start for x in cs} | {first.start - 60.0})
            checks.append(Check("cash_confirmation_rollover", self.linked_confirms("s1", direction, minutes), True,
                                self.linked_confirms("s1", direction, minutes)))
        return checks

    def _needs_cash_confirmation(self, p: Params) -> bool:
        return self.inst.kind == Kind.FUT and self.inst.rollover and p.rollover_requires_cash_confirmation

    # ------------------------------------------------------------------ forming candle (provisional)
    def on_update(self, c: Candle, ts: float, ctx: "Ctx") -> list[Event]:
        p = self.params
        s = self.setup
        T = c.running_turnover_cr
        if s is None:
            if T < p.surge_turnover_floor_cr or "S1" in self.prov and self.prov["S1"][0] == c.start:
                return []
            if (c.start < self.cooldown_until or not self.inst.scannable or self.paused or self.stale
                    or c.flags & UNTRADEABLE or not ctx.session.surge1_allowed(c.minute, p)
                    or self.near_band(c.close, p)[0]):
                return []
            dirs = (1, -1) if p.short_setup_enabled else (1,)
            for d in dirs:
                vc = view(c, d)
                ev = rules.provisional_s1_checks(vc, self.hist(d), self.b_sum([c], p), p)
                if ev.passed:
                    self.prov["S1"] = (c.start, d)
                    return [self._event("provisional", ts, None, reason=Reason.PROVISIONAL.value,
                                        alert=AlertType.S1_PROVISIONAL.value, provisional=True, direction=d,
                                        values={"candle": fmt_hm(c.start), **ev.metrics,
                                                "checks": ev.checks_dicts()})]
            return []
        if s.state == State.ARMED:
            if T < p.s2_floor_cr or ("S2" in self.prov and self.prov["S2"][0] == c.start):
                return []
            if (self.stale or c.flags & UNTRADEABLE or not ctx.session.entry_allowed(c.minute, p)
                    or self.near_band(c.close, p)[0]):
                return []
            vc = view(c, s.direction)
            hist = self.hist(s.direction)
            trigger = self._trigger(hist, p)
            ev = rules.provisional_s2_checks(vc, s.s1_turnover, self.b_sum([c], p), trigger, p)
            if ev.passed:
                self.prov["S2"] = (c.start, s.direction)
                vals = {"candle": fmt_hm(c.start), **ev.metrics, "trigger": s.real(trigger),
                        "ltp": s.real(vc.close), "checks": ev.checks_dicts()}
                return [self._event("provisional", ts, s, reason=Reason.PROVISIONAL.value,
                                    alert=AlertType.S2_PROVISIONAL.value, provisional=True,
                                    grade=(s.grade or {}).get("grade"), score=(s.grade or {}).get("score"),
                                    values=vals)]
        return []

    # ------------------------------------------------------------------ candle close
    def on_close(self, c: Candle, ctx: "Ctx") -> list[Event]:
        """Evaluate the closed candle, then append it to history."""
        events: list[Event] = []
        s = self.setup
        if s is None:
            events += self._idle_close(c, ctx)
        elif s.state in LIVE_STATES:
            events += self._live_close(c, ctx)
        self.commit(c)
        self.prov = {k: v for k, v in self.prov.items() if v[0] > c.start}
        return events

    # -- IDLE ---------------------------------------------------------------
    def _idle_close(self, c: Candle, ctx: "Ctx") -> list[Event]:
        p = self.params
        if self.inst.kind == Kind.IDX:
            return []
        events: list[Event] = []
        dirs = (1, -1) if p.short_setup_enabled else (1,)
        prov = self.prov.get("S1")
        prov_eval: Evaluation | None = None
        started = False
        for d in dirs:
            hist = self.hist(d)
            vc = view(c, d)
            for k in range(1, p.surge_window_candles + 1):
                if k == 1:
                    win_view, real_cs, prev = [vc], [c], hist
                else:
                    if len(self.candles) < k - 1 or self.candles[-1].start != c.start - 60.0:
                        break
                    win_view = hist[-(k - 1):] + [vc]
                    real_cs = self.candles[-(k - 1):] + [c]
                    prev = hist[:-(k - 1)]
                win = rules.merge_window(win_view)
                ev = rules.surge1_checks(win, prev, self.b_sum(real_cs, p), k, p)
                full = evaluation(ev.checks + self._s1_eligibility(real_cs, c, d, p, ctx), ev.metrics)
                if prov is not None and prov[0] == c.start and prov[1] == d and prov_eval is None:
                    prov_eval = full
                if full.passed:
                    events += self._start_setup(c, d, k, win, prev, real_cs, full, ctx)
                    started = self.setup is not None
                    break
            if started:
                break
        if prov is not None and prov[0] == c.start and not started:
            failed = prov_eval.failed if prov_eval is not None else ["not_evaluated"]
            events.append(self._event("cancel", ctx.close_ts, None, reason=Reason.PROVISIONAL_CANCELLED.value,
                                      alert=AlertType.S1_CANCELLED.value, direction=prov[1],
                                      values={"candle": fmt_hm(c.start), "failed": failed,
                                              "turnover_cr": c.turnover_cr,
                                              "checks": prov_eval.checks_dicts() if prov_eval else []}))
        return events

    def _start_setup(self, c: Candle, d: int, k: int, win: Candle, prev: list[Candle], real_cs: list[Candle],
                     full: Evaluation, ctx: "Ctx") -> list[Event]:
        p = self.params
        lookback = prev[-p.leg_start_lookback_candles:] if p.leg_start_lookback_candles > 0 else []
        leg_start = min([x.low for x in lookback] + [win.low])
        floor = win.low if p.pullback_floor == "s1_low" else (win.open + win.close) / 2.0
        sid = f"{self.inst.token}-{ctx.day.date.strftime('%Y%m%d')}-{fmt_hm(win.start).replace(':', '')}-{'L' if d > 0 else 'S'}"
        s = Setup(id=sid, token=self.inst.token, symbol=self.inst.symbol, display=self.inst.display,
                  kind=self.inst.kind.value, direction=d, state=State.SURGE_1, s1_start=win.start,
                  s1_confirmed_ts=ctx.close_ts, s1_open=win.open, s1_high=win.high, s1_low=win.low,
                  s1_close=win.close, s1_turnover=win.turnover_cr, s1_turnover_first=win.turnover_cr,
                  s1_rvol=full.metrics.get("rvol"), s1_buy_share=win.buy_share, s1_move_pct=full.metrics.get("move_pct"),
                  s1_window=k, leg_start=leg_start, H=win.high, h_start=c.start, floor=floor,
                  expire_at=win.start + p.setup_expiry_minutes * 60.0, oi_at_h=c.oi_close,
                  last_close=win.close)
        s.checks["s1"] = full.checks_dicts()
        s.s1_metrics = dict(full.metrics)
        # tags
        if win.flags & Flag.BLOCK_WINDOW:
            s.add_tag("block window")
        if win.flags & Flag.FALLBACK:
            s.add_tag("turnover estimated")
        if ctx.day.events.is_event_day(ctx.day.date, self.inst.underlying):
            s.add_tag("event day")
        if ctx.day.events.is_index_event(ctx.day.date, self.inst.underlying):
            s.add_tag("index event")
        if self.inst.rollover:
            s.add_tag("rollover")
        # OI reading over the window ending with the surge candle (futures)
        oi_reading = None
        if self.inst.is_future:
            dpct = rules.oi_change_pct(self._oi_window(c, max(p.oi_window_candles, len(real_cs))))
            oi_reading = rules.oi_reading_surge(dpct, p)
            s.oi["s1"] = {"change_pct": dpct, "reading": oi_reading, "label": rules.oi_label(oi_reading, d)}
            if oi_reading == "covering":
                s.add_tag(rules.oi_label("covering", d))
        # cash–futures confirmation (same or previous minute; next minute upgrades later)
        minutes = sorted({x.start for x in real_cs} | {real_cs[0].start - 60.0})
        s.cash_fut["s1"] = self.linked_confirms("s1", d, minutes)
        grade = self._grade_s1(s, win, prev, ctx, oi_reading)
        s.grades["s1"] = grade
        vals = {"s1": {**s.real_ohlc(win.open, win.high, win.low, win.close), "turnover_cr": win.turnover_cr,
                       "window": k},
                "metrics": full.metrics, "checks": full.checks_dicts(), "oi": s.oi.get("s1"),
                "cash_fut": s.cash_fut["s1"], "grade_detail": grade, "tags": list(s.tags)}
        if p.event_day_mode == "require_a" and "event day" in s.tags and grade["grade"] != "A":
            return [self._event("transition", ctx.close_ts, s, from_state=State.IDLE.value,
                                to_state=State.IDLE.value, reason=Reason.S1_REJECTED_GRADE.value,
                                grade=grade["grade"], score=grade["score"], values=vals)]
        self.setup = s
        self.eng.setups[s.id] = s
        return [self._transition(s, ctx.close_ts, State.IDLE, State.SURGE_1, Reason.S1_CONFIRMED.value,
                                 AlertType.S1_CONFIRMED.value, vals, grade)]

    # -- live setup -----------------------------------------------------------
    def _live_close(self, c: Candle, ctx: "Ctx") -> list[Event]:
        s = self.setup
        assert s is not None
        p = self.params
        d = s.direction
        hist = self.hist(d)
        vc = view(c, d)
        events: list[Event] = []
        untradeable = bool(c.flags & UNTRADEABLE)
        s.last_close = vc.close

        # 1. continuation: another full Surge 1 candle before the pullback starts
        if s.state == State.SURGE_1 and not untradeable:
            ev = rules.surge1_checks(vc, hist, self.b_sum([c], p), 1, p)
            full = evaluation(ev.checks + self._s1_eligibility([c], c, d, p, ctx, continuation=True), ev.metrics)
            if full.passed:
                if vc.high > s.H:
                    s.H, s.h_start, s.low_since_h, s.oi_at_h = vc.high, c.start, None, c.oi_close
                s.s1_turnover = max(s.s1_turnover, vc.turnover_cr)
                events.append(self._transition(s, ctx.close_ts, State.SURGE_1, State.SURGE_1,
                                               Reason.S1_CONTINUATION.value, None,
                                               {"candle": fmt_hm(c.start), "metrics": full.metrics}))
                return events + self._check_expiry(c, ctx)

        # 2. swing statistics
        new_high = vc.high > s.H
        if new_high and s.state != State.ARMED:
            frm = s.state
            s.H, s.h_start, s.low_since_h, s.oi_at_h = vc.high, c.start, None, c.oi_close
            if frm == State.PULLBACK:
                s.state = State.SURGE_1
                s.pb_start, s.pb_candles, s.pb_turnover_sum, s.pb_turnover_n, s.pb_vol, s.pb_sell = None, 0, 0.0, 0, 0, 0
                events.append(self._transition(s, ctx.close_ts, State.PULLBACK, State.SURGE_1, Reason.NEW_HIGH.value,
                                               None, {"candle": fmt_hm(c.start), "new_H": s.real(vc.high)}))
        else:
            s.low_since_h = vc.low if s.low_since_h is None else min(s.low_since_h, vc.low)
        s.L = s.low_since_h
        span = s.H - s.leg_start
        s.retracement = (s.H - s.L) / span if (s.L is not None and span > 0) else 0.0
        if s.state in (State.PULLBACK, State.ARMED):
            self._add_pullback_candle(s, vc)
        if self.inst.is_future and s.state in (State.PULLBACK, State.ARMED) and s.oi_at_h and c.oi_close:
            dpct = (c.oi_close - s.oi_at_h) / s.oi_at_h * 100.0
            reading = rules.oi_reading_pullback(dpct, p)
            s.oi["pullback"] = {"change_pct": dpct, "reading": reading, "label": rules.oi_label(reading, d)}
            if reading == "heavy_unwinding":
                s.add_tag(rules.oi_label("heavy_unwinding", d))

        # 3. invalidation (checked before the Surge 2 test on the same candle)
        inv = self._invalidation(c, vc, hist, s, p, ctx)
        if inv is not None:
            reason, vals = inv
            events += self._cancel_prov_s2(c, ctx, f"invalidated: {reason}")
            return events + self._end(s, State.INVALIDATED, reason, vals, c, ctx)

        # 4. Surge 2 (entry)
        if s.state == State.ARMED:
            trigger = self._trigger(hist, p)
            ev2 = rules.surge2_checks(vc, s.s1_turnover, self.b_sum([c], p), trigger, p)
            elig = [Check("entry_window", fmt_minute(c.minute), f"< {p.last_entry_time}",
                          ctx.session.entry_allowed(c.minute, p))] + self._common_checks([c], c, p, ctx)
            if self._needs_cash_confirmation(p):
                ok = self.linked_confirms("s2", d, [c.start - 60.0, c.start])
                elig.append(Check("cash_confirmation_rollover", ok, True, ok))
            full2 = evaluation(ev2.checks + elig, ev2.metrics)
            s.checks["s2"] = full2.checks_dicts()
            if full2.passed:
                return events + self._entry(c, vc, hist, s, full2, trigger, p, ctx)
            events += self._cancel_prov_s2(c, ctx, None, full2)

        # 5. pullback start: lower high than H and a close below the previous close
        if s.state == State.SURGE_1 and not new_high and hist and vc.high < s.H and vc.close < hist[-1].close:
            s.state = State.PULLBACK
            s.pb_start = c.start
            self._add_pullback_candle(s, vc)
            events.append(self._transition(s, ctx.close_ts, State.SURGE_1, State.PULLBACK,
                                           Reason.PULLBACK_STARTED.value, None, {"candle": fmt_hm(c.start)}))

        # 6. arm
        if s.state == State.PULLBACK:
            arm = self._arm_checks(s, p)
            s.checks["armed"] = arm.checks_dicts()
            if arm.passed:
                s.state = State.ARMED
                s.armed_ts = ctx.close_ts
                grade = self._grade_armed(s, vc, ctx)
                s.grades["armed"] = grade
                s.P = self._trigger(hist + [vc], p)
                events.append(self._transition(s, ctx.close_ts, State.PULLBACK, State.ARMED, Reason.ARMED.value,
                                               AlertType.ARMED.value,
                                               {"checks": arm.checks_dicts(), "grade_detail": grade,
                                                "oi": s.oi.get("pullback")}, grade))

        # 7. trigger level for the display (now including this candle)
        s.P = self._trigger(hist + [vc], p)
        return events + self._check_expiry(c, ctx)

    def _add_pullback_candle(self, s: Setup, vc: Candle) -> None:
        s.pb_candles += 1
        if not (vc.flags & UNTRADEABLE):
            s.pb_turnover_sum += vc.turnover_cr
            s.pb_turnover_n += 1
            s.pb_vol += vc.volume
            s.pb_sell += vc.sell_vol

    def _oi_window(self, c: Candle, n: int) -> list[Candle]:
        """The ``n`` candles ending with ``c`` (``c`` is not yet in history)."""
        return (self.candles[-(n - 1):] if n > 1 else []) + [c]

    @staticmethod
    def _trigger(hist: list[Candle], p: Params) -> float | None:
        n = p.trigger_lookback_candles
        if not hist or n <= 0:
            return None
        return max(x.high for x in hist[-n:])

    def _arm_checks(self, s: Setup, p: Params) -> Evaluation:
        return evaluation([
            Check("retracement_min", s.retracement, p.pullback_min_retrace, s.retracement >= p.pullback_min_retrace),
            Check("pullback_candles", s.pb_candles, p.pullback_min_candles, s.pb_candles >= p.pullback_min_candles),
            Check("pullback_turnover_ratio", s.pb_ratio, p.pullback_max_turnover_ratio,
                  s.pb_ratio <= p.pullback_max_turnover_ratio),
            Check("above_floor", s.real(s.L), s.real(s.floor), True),
        ])

    def _invalidation(self, c: Candle, vc: Candle, hist: list[Candle], s: Setup, p: Params,
                      ctx: "Ctx") -> tuple[str, dict] | None:
        if vc.close < s.floor:
            return Reason.FLOOR_BROKEN.value, {"close": s.real(vc.close), "floor": s.real(s.floor),
                                               "floor_mode": p.pullback_floor}
        if s.retracement > p.pullback_max_retrace:
            return Reason.MAX_RETRACE.value, {"retracement": s.retracement, "max": p.pullback_max_retrace,
                                              "low": s.real(s.L)}
        hit, vals = rules.is_distribution(vc, s.s1_turnover, p)
        if hit:
            return Reason.DISTRIBUTION.value, {**vals, "s1_turnover_cr": s.s1_turnover,
                                               "threshold_ratio": p.distribution_turnover_ratio,
                                               "sell_share_min": p.distribution_sell_share_min}
        if self.inst.is_future and hist and vc.close < hist[-1].close and not (c.flags & UNTRADEABLE):
            window = self._oi_window(c, p.oi_window_candles)
            dpct = rules.oi_change_pct(window)
            if (dpct is not None and dpct >= p.oi_change_min_pct
                    and vc.turnover_cr >= p.short_buildup_turnover_ratio * s.s1_turnover):
                return Reason.SHORT_BUILDUP.value, {"oi_change_pct": dpct, "turnover_cr": vc.turnover_cr,
                                                    "turnover_vs_s1": vc.turnover_cr / s.s1_turnover}
        if self.circuit_hit(c):
            return Reason.CIRCUIT.value, {"high": c.high, "low": c.low, "upper_band": self.inst.upper_band,
                                          "lower_band": self.inst.lower_band}
        if ctx.day.events.is_halted(ctx.day.date, self.inst.underlying):
            return Reason.HALT.value, {}
        return None

    def _entry(self, c: Candle, vc: Candle, hist: list[Candle], s: Setup, full2: Evaluation,
               trigger: float | None, p: Params, ctx: "Ctx") -> list[Event]:
        atr = self.atr.current
        s.extended = atr is not None and vc.close > s.H + p.extended_atr_multiple * atr
        if s.extended:
            s.add_tag("extended")
        tick = self.inst.tick_size or 0.05
        entry = vc.close if p.entry_mode == "close" else round_up(vc.high + tick, tick)
        plan = compute_plan(entry, s.L if s.L is not None else s.s1_low, s.s1_low, atr, p, self.inst, s.direction)
        oi_reading = None
        if self.inst.is_future:
            dpct = rules.oi_change_pct(self._oi_window(c, p.oi_window_candles))
            oi_reading = rules.oi_reading_surge(dpct, p)
            s.oi["s2"] = {"change_pct": dpct, "reading": oi_reading, "label": rules.oi_label(oi_reading, s.direction)}
            if oi_reading == "covering":
                s.add_tag(rules.oi_label("covering", s.direction))
        s.cash_fut["s2"] = self.linked_confirms("s2", s.direction, [c.start - 60.0, c.start])
        s.s2 = {"time": fmt_hm(c.start), "turnover_cr": vc.turnover_cr, "rvol": full2.metrics.get("rvol"),
                **s.real_ohlc(vc.open, vc.high, vc.low, vc.close), "trigger": s.real(trigger),
                "buy_share": vc.buy_share, "extended": s.extended, "atr": atr,
                "turnover_vs_s1": full2.metrics.get("turnover_vs_s1")}
        if not (p.min_risk_pct <= plan.risk_pct <= p.max_risk_pct):
            return self._end(s, State.INVALIDATED, Reason.RISK_OUT_OF_RANGE.value,
                             {"plan": plan.as_dict(), "min_risk_pct": p.min_risk_pct, "max_risk_pct": p.max_risk_pct,
                              "s2": s.s2, "checks": full2.checks_dicts()}, c, ctx)
        grade = self._grade_entry(s, vc, full2, oi_reading, ctx)
        s.grades["entry"] = grade
        suppressed = self.eng.trades.book.tripped(p)
        if suppressed is None and self.eng.trades.open_positions() >= p.max_open_positions:
            suppressed = "max_open_positions"
        if (suppressed is None and p.event_day_mode == "require_a" and "event day" in s.tags
                and grade["grade"] != "A"):
            suppressed = "event_day_requires_grade_a"
        s.suppressed = suppressed
        s.state = State.ENTRY
        s.entry_ts = ctx.close_ts
        s.plan = plan.as_dict()
        vals = {"s2": s.s2, "plan": s.plan, "checks": full2.checks_dicts(), "grade_detail": grade,
                "oi": s.oi.get("s2"), "oi_pullback": s.oi.get("pullback"), "cash_fut": dict(s.cash_fut),
                "suppressed": suppressed, "tags": list(s.tags)}
        ev = [self._transition(s, ctx.close_ts, State.ARMED, State.ENTRY, Reason.S2_ENTRY.value,
                               None if suppressed else AlertType.ENTRY.value, vals, grade)]
        trade, tev = self.eng.trades.open(
            setup_id=s.id, plan=plan, inst=self.inst, ts=ctx.close_ts, candle_start=c.start,
            s2_high_view=vc.high, s1_turnover=s.s1_turnover, shadow=suppressed is not None,
            suppressed=suppressed, grade=grade["grade"], score=grade["score"], params=p,
            cash_fut=bool(s.cash_fut.get("s2") or s.cash_fut.get("s1")), tags=s.tags)
        s.trade_id = trade.id
        self.prov.pop("S2", None)
        return ev + tev

    def _cancel_prov_s2(self, c: Candle, ctx: "Ctx", why: str | None,
                        full2: Evaluation | None = None) -> list[Event]:
        prov = self.prov.get("S2")
        if prov is None or prov[0] != c.start:
            return []
        self.prov.pop("S2", None)
        failed = [why] if why else (full2.failed if full2 else [])
        return [self._event("cancel", ctx.close_ts, self.setup, reason=Reason.PROVISIONAL_CANCELLED.value,
                            alert=AlertType.S2_CANCELLED.value,
                            values={"candle": fmt_hm(c.start), "failed": failed, "turnover_cr": c.turnover_cr,
                                    "checks": full2.checks_dicts() if full2 else []})]

    def _check_expiry(self, c: Candle, ctx: "Ctx") -> list[Event]:
        s = self.setup
        if s is None or s.state not in LIVE_STATES:
            return []
        p = self.params
        if c.end >= s.expire_at:
            return self._end(s, State.EXPIRED, Reason.TIMEOUT.value,
                             {"expire_at": fmt_hm(s.expire_at), "minutes": p.setup_expiry_minutes}, c, ctx)
        if minute_of_day(c.end) >= p.last_entry_min:
            return self._end(s, State.EXPIRED, Reason.LAST_ENTRY_TIME.value,
                             {"last_entry_time": p.last_entry_time}, c, ctx)
        return []

    def _end(self, s: Setup, state: State, reason: str, values: dict, c: Candle | None,
             ctx: "Ctx | None", ts: float | None = None) -> list[Event]:
        p = self.params
        frm = s.state
        when = ts if ts is not None else (ctx.close_ts if ctx is not None else (c.end if c else 0.0))
        s.state = state
        s.end_ts = when
        s.end_reason = reason
        base = c.end if c is not None else when
        self.cooldown_until = base + p.cooldown_minutes * 60.0
        self.setup = None
        self.prov.pop("S2", None)
        self.eng.archive_setup(s)
        alert = {State.INVALIDATED: AlertType.INVALIDATED.value, State.EXPIRED: AlertType.EXPIRED.value}.get(state)
        return [self._transition(s, when, frm, state, reason, alert,
                                 {**values, "cooldown_until": fmt_hm(self.cooldown_until)})]

    # -- external events -------------------------------------------------------
    def on_stale(self, ts: float) -> list[Event]:
        self.stale = True
        s = self.setup
        if s is not None and s.state in LIVE_STATES:
            return self._end(s, State.INVALIDATED, Reason.STALE.value,
                             {"stale_after_seconds": self.params.stale_after_seconds}, None, None, ts=ts)
        return []

    def on_trade_closed(self, trade, ts: float) -> list[Event]:
        s = self.setup
        if s is None or s.trade_id != trade.id:
            return []
        state = State.EXPIRED if trade.status == "cancelled" else State.COMPLETED
        reason = Reason.BUY_STOP_NOT_TRIGGERED.value if trade.status == "cancelled" else Reason.TRADE_CLOSED.value
        return self._end(s, state, reason, {"trade": trade.row()}, None, None, ts=ts)

    def check_late_confirmation(self, minute_start: float, ctx: "Ctx") -> list[Event]:
        """The linked instrument surged one minute after our S1/S2 (adjacent minute)."""
        s = self.setup
        if s is None:
            return []
        out = []
        if s.state in LIVE_STATES and not s.cash_fut.get("s1") and s.s1_start + 60.0 * s.s1_window == minute_start:
            if self.linked_confirms("s1", s.direction, [minute_start]):
                s.cash_fut["s1"] = True
                s.cash_fut["s1_late"] = True
                out.append(self._event("update", ctx.close_ts, s, reason=Reason.CASH_FUT_LATE.value,
                                       values={"stage": "s1"}))
        return out

    # ------------------------------------------------------------------ grading
    def _breakout(self, win: Candle, prev: list[Candle], ctx: "Ctx", p: Params) -> float:
        prior_high = max((x.high for x in prev), default=None)
        if prior_high is not None and win.high > prior_high:
            return 1.0
        or_end = ctx.session.cont_start + p.opening_range_minutes
        if win.minute >= or_end:
            or_high = max((x.high for x in prev if x.minute < or_end), default=None)
            if or_high is not None and win.close > or_high:
                return 1.0
        return 0.0

    def _grade_s1(self, s: Setup, win: Candle, prev: list[Candle], ctx: "Ctx", oi_reading: str | None) -> dict:
        p = self.params
        g = self.eng.config.grading
        comps = {
            "rvol": rvol_component(s.s1_rvol, p.surge_rvol_min, g),
            "buy_share": buy_share_component(win.buy_share, p.buy_share_min, g),
            "vwap": None if win.vwap is None else (1.0 if win.close > win.vwap else 0.0),
            "breakout": self._breakout(win, prev, ctx, p),
            "oi": OI_SURGE_SCORE.get(oi_reading) if (self.inst.is_future and oi_reading) else None,
            "cash_fut": 1.0 if s.cash_fut.get("s1") else 0.0,
            "regime": regime_component(ctx.regime, s.direction, p),
            "book": book_component(win.total_buy_qty / win.total_sell_qty if win.total_sell_qty else None, g),
        }
        return compute_score(comps, self._penalties(s), g, p)

    def _grade_armed(self, s: Setup, vc: Candle, ctx: "Ctx") -> dict:
        p = self.params
        g = self.eng.config.grading
        s1 = s.grades.get("s1", {}).get("components", {})
        pb_oi = (s.oi.get("pullback") or {}).get("reading")
        comps = {
            "rvol": s1.get("rvol"), "buy_share": s1.get("buy_share"), "breakout": s1.get("breakout"),
            "vwap": None if vc.vwap is None else (1.0 if vc.close > vc.vwap else 0.0),
            "oi": OI_PULLBACK_SCORE.get(pb_oi) if (self.inst.is_future and pb_oi) else s1.get("oi"),
            "cash_fut": 1.0 if s.cash_fut.get("s1") else 0.0,
            "dryup": ramp(p.pullback_max_turnover_ratio - s.pb_ratio, 0.0, p.pullback_max_turnover_ratio),
            "pullback_sell": None if s.pb_sell_share is None else (
                1.0 if s.pb_sell_share <= p.pullback_max_sell_share_scored else 0.0),
            "regime": regime_component(ctx.regime, s.direction, p),
            "book": book_component(vc.total_buy_qty / vc.total_sell_qty if vc.total_sell_qty else None, g),
        }
        return compute_score(comps, self._penalties(s), g, p)

    def _grade_entry(self, s: Setup, vc: Candle, full2: Evaluation, oi_reading: str | None, ctx: "Ctx") -> dict:
        p = self.params
        g = self.eng.config.grading
        s1 = s.grades.get("s1", {}).get("components", {})
        armed = s.grades.get("armed", {}).get("components", {})

        def avg(*xs: float | None) -> float | None:
            vals = [x for x in xs if x is not None]
            return sum(vals) / len(vals) if vals else None

        oi_comp = None
        if self.inst.is_future:
            pb = (s.oi.get("pullback") or {}).get("reading")
            oi_comp = avg(s1.get("oi"), OI_PULLBACK_SCORE.get(pb) if pb else None,
                          OI_SURGE_SCORE.get(oi_reading) if oi_reading else None)
        cf = 1.0 if s.cash_fut.get("s2") else (0.5 if s.cash_fut.get("s1") else 0.0)
        comps = {
            "rvol": avg(s1.get("rvol"), rvol_component(full2.metrics.get("rvol"), p.s2_rvol_min, g)),
            "buy_share": avg(s1.get("buy_share"), buy_share_component(vc.buy_share, p.buy_share_min, g)),
            "breakout": s1.get("breakout"),
            "vwap": None if vc.vwap is None else (1.0 if vc.close > vc.vwap else 0.0),
            "oi": oi_comp,
            "cash_fut": cf,
            "dryup": armed.get("dryup"),
            "pullback_sell": armed.get("pullback_sell"),
            "regime": regime_component(ctx.regime, s.direction, p),
            "book": book_component(vc.total_buy_qty / vc.total_sell_qty if vc.total_sell_qty else None, g),
        }
        return compute_score(comps, self._penalties(s), g, p)

    def _penalties(self, s: Setup) -> list[str]:
        mapping = {"block window": "block_window", "event day": "event_day", "index event": "index_event",
                   "extended": "extended"}
        out = [mapping[t] for t in s.tags if t in mapping]
        cov = rules.oi_label("covering", s.direction)
        if cov in s.tags:
            out.append("short_covering")
        if rules.oi_label("heavy_unwinding", s.direction) in s.tags:
            out.append("heavy_unwind")
        return out


def _flag_str(flags: Flag) -> str:
    return ",".join(f.name.lower() for f in Flag if f and f.name and (flags & f) and f != Flag.NONE)
