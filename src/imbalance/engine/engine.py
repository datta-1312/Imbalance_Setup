"""Screener engine: runs every instrument's state machine over the candle stream.

Deterministic and free of I/O: the same calls in the same order produce the same
events, which is what makes live mode and replay/backtest identical (§8, §11).

Per closed minute (all instruments together):
  1. volume-surge flags for cash–futures confirmation, market regime (NIFTY)
  2. late (adjacent-minute) cash–futures confirmations
  3. each instrument's state machine on its candle
  4. open trades: time stop, sell-surge exit, end-of-day, trailing
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..baseline import Baseline
from ..calendar import DayInfo, SessionRules
from ..config import AppConfig, ConfigManager, Params
from ..instruments import Universe
from ..models import Candle, Instrument, Kind
from ..timeutil import fmt_hm
from .tracker import Setup, Tracker
from .trades import Trade, TradeManager
from .types import END_STATES, LIVE_STATES, AlertType, Event, State


@dataclass
class Ctx:
    now: float
    close_ts: float
    session: SessionRules
    day: DayInfo
    regime: float | None


class _StaticConfig:
    def __init__(self, cfg: AppConfig):
        self.config = cfg
        self.version = cfg.version()

    def params_for(self, symbol: str, kind: str) -> Params:
        return self.config.params_for(symbol, kind)


class ScreenerEngine:
    def __init__(self, config: ConfigManager | AppConfig, universe: Universe, baselines: dict[int, Baseline],
                 day: DayInfo, mode: str = "ticks"):
        self._cfg = _StaticConfig(config) if isinstance(config, AppConfig) else config
        self.universe = universe
        self.baselines = baselines
        self.day = day
        self.mode = mode
        self.setups: dict[str, Setup] = {}
        self.archive: list[Setup] = []
        self.regime: dict[str, float | None] = {}
        self.trades = TradeManager(lambda: self.config, self._trade_closed)
        self.trackers: dict[int, Tracker] = {tok: Tracker(inst, self)
                                             for tok, inst in sorted(universe.instruments.items())}
        self._regime_names = {tok: name for name, tok in universe.regime.items()}
        self._seq = 0
        self.last_close_ts: float | None = None

    # ------------------------------------------------------------------ config
    @property
    def config(self) -> AppConfig:
        return self._cfg.config

    @property
    def config_version(self) -> str:
        return self._cfg.version

    def params_for(self, inst: Instrument) -> Params:
        return self._cfg.params_for(inst.underlying, inst.kind.value)

    def ctx(self, now: float, close_ts: float | None = None) -> Ctx:
        return Ctx(now=now, close_ts=now if close_ts is None else close_ts, session=self.day.session,
                   day=self.day, regime=self.regime.get("NIFTY"))

    def _number(self, events: list[Event]) -> list[Event]:
        for e in events:
            self._seq += 1
            e.seq = self._seq
        return events

    # ------------------------------------------------------------------ inputs
    def on_update(self, c: Candle, ts: float) -> list[Event]:
        """A forming candle changed (one snapshot): provisional alerts."""
        tr = self.trackers.get(c.token)
        if tr is None:
            return []
        ev = tr.on_update(c, ts, self.ctx(ts))
        if c.token in self.trades.active:
            ev += self.trades.provisional_exit_warning(c, tr.params, ts)
        return self._number(ev) if ev else ev

    def on_price(self, token: int, price: float, ts: float) -> list[Event]:
        """Stops and targets of open trades, checked on every snapshot."""
        if token not in self.trades.active:
            return []
        ev = self.trades.on_price(token, price, ts, self.trackers[token].params)
        return self._number(ev) if ev else ev

    def on_batch(self, minute_start: float, candles: dict[int, Candle], close_ts: float) -> list[Event]:
        """All instruments' candles for one minute, closed together."""
        self.last_close_ts = close_ts
        ctx = self.ctx(close_ts, close_ts)
        tokens = sorted(t for t in candles if t in self.trackers)
        for tok in tokens:
            tr = self.trackers[tok]
            c = candles[tok]
            tr.compute_flags(c)
            name = self._regime_names.get(tok)
            if name is not None:
                ref = c.prev_day_close or tr.prev_day_close
                self.regime[name] = (c.close - ref) / ref * 100.0 if ref else None
        ctx.regime = self.regime.get("NIFTY")
        events: list[Event] = []
        for tok in tokens:
            events += self.trackers[tok].check_late_confirmation(minute_start, ctx)
        for tok in tokens:
            events += self.trackers[tok].on_close(candles[tok], ctx)
        for tok in tokens:
            trade = self.trades.active.get(tok)
            if trade is None:
                continue
            tr = self.trackers[tok]
            events += self.trades.on_candle(candles[tok], tr.hist(trade.direction), tr.params, self.day.session,
                                            self.mode, close_ts)
        return self._number(events)

    def on_status(self, token: int, status: str, ts: float) -> list[Event]:
        tr = self.trackers.get(token)
        if tr is None:
            return []
        if status == "stale":
            ev = tr.on_stale(ts)
            ev.append(Event(kind="status", ts=ts, token=token, symbol=tr.inst.symbol, display=tr.inst.display,
                            reason="stale", values={}))
        else:
            tr.stale = False
            ev = [Event(kind="status", ts=ts, token=token, symbol=tr.inst.symbol, display=tr.inst.display,
                        reason="fresh", values={})]
        return self._number(ev)

    def system_event(self, ts: float, reason: str, values: dict[str, Any] | None = None,
                     alert: bool = True) -> list[Event]:
        return self._number([Event(kind="system", ts=ts, reason=reason,
                                   alert=AlertType.SYSTEM.value if alert else None, values=values or {})])

    def end_of_day(self, ts: float) -> list[Event]:
        """Session over: close anything still open at the last price."""
        events: list[Event] = []
        for tok in sorted(self.trades.active):
            tr = self.trackers[tok]
            price = tr.last_price
            if price is not None:
                events += self.trades.force_exit(tok, price, ts, "session_end", tr.params)
        for tok in sorted(self.trackers):
            tr = self.trackers[tok]
            s = tr.setup
            if s is not None and s.state in LIVE_STATES:
                events += tr._end(s, State.EXPIRED, "session_end", {}, None, None, ts=ts)
        return self._number(events)

    # ------------------------------------------------------------------ callbacks
    def _trade_closed(self, trade: Trade, ts: float) -> list[Event]:
        tr = self.trackers.get(trade.token)
        return tr.on_trade_closed(trade, ts) if tr else []

    def archive_setup(self, s: Setup) -> None:
        self.archive.append(s)

    # ------------------------------------------------------------------ controls
    def set_paused(self, token: int, paused: bool) -> bool:
        tr = self.trackers.get(token)
        if tr is None:
            return False
        tr.paused = paused
        return True

    # ------------------------------------------------------------------ views
    def snapshot(self, now: float) -> dict[str, Any]:
        live = [tr.setup for tok, tr in sorted(self.trackers.items()) if tr.setup is not None]
        surge1 = [s.row(now) for s in live if s.state == State.SURGE_1]
        pullback = [s.row(now) for s in live if s.state in (State.PULLBACK, State.ARMED)]
        entries = []
        archive = []
        for t in self.trades.trades.values():
            row = t.row()
            s = self.setups.get(t.setup_id)
            if s is not None:
                row["setup"] = {"s1_time": fmt_hm(s.s1_start), "s1_turnover": s.s1_turnover, "s1_rvol": s.s1_rvol,
                                "retracement": s.retracement, "pb_ratio": s.pb_ratio, "s2": s.s2,
                                "oi": s.oi, "cash_fut": s.cash_fut, "tags": s.tags, "state": s.state.value}
            (archive if t.shadow else entries).append(row)
        for s in self.archive:
            if s.state in (State.INVALIDATED, State.EXPIRED) and not s.trade_id:
                archive.append(s.row(now))
        params = self.config.base_params()
        return {
            "now": now,
            "date": self.day.date.isoformat(),
            "surge1": surge1,
            "pullback": pullback,
            "entries": entries,
            "archive": archive,
            "guardrails": self.trades.book.as_dict(params, self.trades.open_positions()),
            "regime": dict(self.regime),
            "config_version": self.config_version,
            "counts": {
                "instruments": len(self.trackers),
                "scannable": sum(1 for tr in self.trackers.values() if tr.inst.scannable),
                "live_setups": len(live),
                "stale": sum(1 for tr in self.trackers.values() if tr.stale),
            },
        }

    def setup_detail(self, setup_id: str) -> dict[str, Any] | None:
        s = self.setups.get(setup_id)
        if s is None:
            return None
        return {"setup": s.row(self.last_close_ts), "instrument": self.instrument_detail(s.token)}

    def instrument_detail(self, token: int) -> dict[str, Any] | None:
        tr = self.trackers.get(token)
        if tr is None:
            return None
        b = tr.baseline
        candles = [{
            "t": c.start, "o": c.open, "h": c.high, "l": c.low, "c": c.close, "turnover": round(c.turnover_cr, 4),
            "baseline": (round(b.b(c.minute), 4) if b and b.b(c.minute) is not None else None),
            "vwap": c.vwap, "oi": c.oi_close, "buy_share": round(c.buy_share, 3), "flags": int(c.flags),
        } for c in tr.candles]
        trade = self.trades.active.get(token)
        return {
            "token": token, "symbol": tr.inst.symbol, "display": tr.inst.display, "kind": tr.inst.kind.value,
            "underlying": tr.inst.underlying, "linked_token": tr.inst.linked_token, "scannable": tr.inst.scannable,
            "scan_block_reason": tr.inst.scan_block_reason, "lot_size": tr.inst.lot_size,
            "atr": tr.atr.current, "stale": tr.stale, "paused": tr.paused, "candles": candles,
            "setup_id": tr.setup.id if tr.setup else None, "trade": trade.row() if trade else None,
            "setups_today": [sid for sid, s in self.setups.items() if s.token == token],
        }

    def all_events_states(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for s in self.setups.values():
            out[s.state.value] = out.get(s.state.value, 0) + 1
        return out


def ended(states: Iterable[State]) -> bool:
    return all(s in END_STATES for s in states)
