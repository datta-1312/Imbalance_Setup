"""Backtest runners that call the same setup engine as live mode (spec §11).

* ``CandleBacktest``: historical 1-minute candles, candle-shape direction, fills at
  candle prices walked O→L→H→C / O→H→L→C, slippage and costs from the config.
* ``TickBacktest``: recorded raw snapshots replayed through the full pipeline.

No look-ahead: baselines for day D use only sessions before D; the universe
for D is what had data (or what ``universe_for`` says) on D.

Each run also simulates the core comparison of §11: entering at Surge 1 and
random entries at the same times of day, with the same risk and exit rules.
"""

from __future__ import annotations

import copy
import logging
import random
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable

from ..baseline import Baseline, build_baseline
from ..calendar import DayInfo, EventCalendar, SessionRules, TradingCalendar
from ..config import AppConfig, Params
from ..engine import Event, ScreenerEngine
from ..engine.risk import TradePlan, compute_plan, round_down
from ..engine.trades import Trade, TradeManager
from ..instruments import Universe
from ..models import Candle, Instrument, Kind, view
from .data import InstrumentHistory, minute_map
from .metrics import TradeRecord, funnel, split_metrics, summarize

log = logging.getLogger(__name__)


@dataclass
class DayResult:
    date: date
    events: list[Event]
    trades: list[TradeRecord]
    s1_trades: list[TradeRecord] = field(default_factory=list)
    random_trades: list[TradeRecord] = field(default_factory=list)


@dataclass
class BacktestResult:
    days: list[DayResult]
    config_version: str
    mode: str

    @property
    def trades(self) -> list[TradeRecord]:
        return [t for d in self.days for t in d.trades]

    def report(self) -> dict:
        alerted = [t for t in self.trades if not t.shadow]
        shadow = [t for t in self.trades if t.shadow]
        s1 = [t for d in self.days for t in d.s1_trades]
        rnd = [t for d in self.days for t in d.random_trades]
        f = funnel(e for d in self.days for e in d.events)
        f["winners"] = sum(1 for t in alerted if t.net_r > 0)
        return {
            "mode": self.mode,
            "config_version": self.config_version,
            "days": len(self.days),
            "first_day": self.days[0].date.isoformat() if self.days else None,
            "last_day": self.days[-1].date.isoformat() if self.days else None,
            "funnel": f,
            "summary": summarize(alerted),
            "suppressed_entries": summarize(shadow),
            "splits": split_metrics(alerted),
            "comparison": {
                "enter_at_surge2": summarize(alerted),
                "enter_at_surge1": summarize(s1),
                "random_same_time_of_day": summarize(rnd),
            },
        }


# --------------------------------------------------------------------------- simulation helper
def simulate_trade(cfg: AppConfig, inst: Instrument, direction: int, entry_candle: Candle, later: list[Candle],
                   stop_view: float, params: Params, session: SessionRules, ref_turnover: float,
                   tag: str) -> Trade | None:
    """One trade with the live exit rules, used by the comparators."""
    ve = view(entry_candle, direction)
    entry = ve.close
    R = entry - stop_view
    if R <= 0:
        return None
    risk_pct = R / abs(entry) * 100.0
    if not (params.min_risk_pct <= risk_pct <= params.max_risk_pct):
        return None
    budget = params.capital * params.risk_per_trade_pct / 100.0
    q = int(budget // R)
    if inst.is_future:
        q = (q // max(1, inst.lot_size)) * max(1, inst.lot_size)
    plan = TradePlan(direction=direction, entry=entry, stop=stop_view, R=R, risk_pct=risk_pct, qty=q, lots=None,
                     t1=round(entry + params.t1_r * R, 6), t2=round(entry + params.t2_r * R, 6),
                     position_value=q * abs(entry), entry_mode="close")
    tm = TradeManager(lambda: cfg, lambda tr, ts: [])
    tr, _ = tm.open(setup_id=f"{tag}-{inst.token}-{int(entry_candle.start)}", plan=plan, inst=inst,
                    ts=entry_candle.end, candle_start=entry_candle.start, s2_high_view=ve.high,
                    s1_turnover=ref_turnover, shadow=False, suppressed=None, grade=None, score=None,
                    params=params, cash_fut=False, tags=[])
    hist: list[Candle] = [ve]
    grace = cfg.session.candle_close_grace_seconds
    for c in later:
        hist.append(view(c, direction))
        tm.on_candle(c, hist, params, session, "ohlc", c.end + grace)
        if not tr.is_active:
            break
    if tr.is_active:
        last = later[-1] if later else entry_candle
        tm.force_exit(inst.token, last.close, last.end + grace, "session_end", params)
    return tr


# --------------------------------------------------------------------------- candle backtest
class CandleBacktest:
    def __init__(self, cfg: AppConfig, history: dict[int, InstrumentHistory], calendar: TradingCalendar | None = None,
                 events: EventCalendar | None = None, universe_for: Callable[[date], set[int]] | None = None,
                 compare: bool = True, baseline_cache: dict | None = None,
                 progress: Callable[[date], None] | None = None):
        self.cfg = cfg
        self.history = history
        self.calendar = calendar
        self.events = events
        self.universe_for = universe_for
        self.compare = compare
        self.cache: dict = baseline_cache if baseline_cache is not None else {}
        self.progress = progress
        self.all_days = sorted({d for h in history.values() for d in h.days})
        self.regime_tokens = {h.inst.underlying: tok for tok, h in history.items() if h.inst.kind == Kind.IDX}

    def trading_days(self, start: date | None = None, end: date | None = None) -> list[date]:
        return [d for d in self.all_days if (start is None or d >= start) and (end is None or d <= end)]

    def baselines_for(self, d: date, params: Params, session: SessionRules) -> dict[int, Baseline]:
        prior = [x for x in self.all_days if x < d][-params.baseline_sessions:]
        out = {}
        for tok, h in self.history.items():
            key = (tok, d, params.baseline_sessions, params.baseline_smoothing_minutes, params.baseline_floor_cr)
            b = self.cache.get(key)
            if b is None:
                sessions = [h.bars(x) for x in prior if h.days.get(x)]
                minutes = range(session.cont_start, session.session_end(h.inst.kind))
                b = build_baseline(tok, sessions, minutes, params.baseline_smoothing_minutes, params.baseline_floor_cr,
                                   params.atr_period, asof=d)
                self.cache[key] = b
            out[tok] = b
        return out

    def run(self, start: date | None = None, end: date | None = None) -> BacktestResult:
        days = []
        for d in self.trading_days(start, end):
            if self.progress:
                self.progress(d)
            days.append(self.run_day(d))
        return BacktestResult(days=days, config_version=self.cfg.version(), mode="candles")

    def run_day(self, d: date) -> DayResult:
        cfg = self.cfg
        params = cfg.base_params()
        day = DayInfo.build(d, cfg.session, self.calendar, self.events)
        session = day.session
        baselines = self.baselines_for(d, params, session)
        allowed = self.universe_for(d) if self.universe_for else None
        insts: dict[int, Instrument] = {}
        for tok, h in self.history.items():
            if not h.days.get(d) or (allowed is not None and tok not in allowed and h.inst.kind != Kind.IDX):
                continue
            inst = copy.copy(h.inst)
            b = baselines.get(tok)
            inst.adtv_cr = b.adtv_cr if b else None
            if inst.kind == Kind.IDX:
                inst.scannable, inst.scan_block_reason = False, "regime_only"
            elif b is None or b.sessions < params.baseline_min_sessions:
                inst.scannable, inst.scan_block_reason = False, "no_baseline"
            elif b.adtv_cr < params.adtv_floor_cr:
                inst.scannable, inst.scan_block_reason = False, "adtv_below_floor"
            insts[tok] = inst
        universe = Universe(date=d, instruments=insts,
                            regime={k: v for k, v in self.regime_tokens.items() if v in insts})
        engine = ScreenerEngine(cfg, universe, baselines, day, mode="ohlc")
        grace = cfg.session.candle_close_grace_seconds
        events: list[Event] = []
        mm = minute_map({t: self.history[t] for t in insts}, d)
        regime_at: dict[float, float | None] = {}
        last = None
        for m, batch in mm.items():
            batch = {t: c for t, c in batch.items() if session.candle_allowed(insts[t].kind, c.minute)}
            if not batch:
                continue
            events += engine.on_batch(m, batch, m + 60.0 + grace)
            regime_at[m + 60.0 + grace] = engine.regime.get("NIFTY")
            last = m
        if last is not None:
            events += engine.end_of_day(last + 120.0)
        trades = []
        for t in engine.trades.trades.values():
            if t.status != "closed":
                continue
            trades.append(TradeRecord.from_trade(t, engine.setups.get(t.setup_id), regime_at.get(t.entry_ts)))
        res = DayResult(date=d, events=events, trades=trades)
        if self.compare:
            day_candles = {tok: [c for c in self.history[tok].days.get(d, [])
                                 if session.candle_allowed(insts[tok].kind, c.minute)] for tok in insts}
            res.s1_trades, res.random_trades = compare_entries(cfg, engine, day_candles, session, d,
                                                               cfg.backtest.random_seed)
        return res


def compare_entries(cfg: AppConfig, engine: ScreenerEngine, day_candles: dict[int, list[Candle]],
                    session: SessionRules, d: date, seed: int) -> tuple[list[TradeRecord], list[TradeRecord]]:
    """The core test of §11: the same day's Surge 1 setups entered at S1, and
    random instruments entered at the same times of day as the S2 entries."""
    s1_out: list[TradeRecord] = []
    rnd_out: list[TradeRecord] = []
    index = {tok: {c.start: i for i, c in enumerate(cs)} for tok, cs in day_candles.items()}
    for s in engine.setups.values():
        tr_ = engine.trackers.get(s.token)
        if tr_ is None:
            continue
        cs = day_candles.get(s.token, [])
        end_start = s.s1_start + 60.0 * (s.s1_window - 1)
        i = index.get(s.token, {}).get(end_start)
        if i is None:
            continue
        params = engine.params_for(tr_.inst)
        atr = _atr_before(cs, i, params, engine.baselines.get(s.token))
        stop = round_down(s.s1_low - params.stop_buffer_atr * (atr or 0.0), tr_.inst.tick_size or 0.05)
        t = simulate_trade(cfg, tr_.inst, s.direction, cs[i], cs[i + 1:], stop, params, session, s.s1_turnover, "S1")
        if t is not None and t.status == "closed":
            s1_out.append(TradeRecord.from_trade(t, s, source="s1"))
    rng = random.Random(f"{seed}-{d.isoformat()}")
    tokens = sorted(t for t, i in engine.universe.instruments.items() if i.scannable and day_candles.get(t))
    n_rand = cfg.backtest.random_entries_per_signal
    for t in sorted(engine.trades.trades.values(), key=lambda x: x.id):
        if t.shadow or t.status != "closed":
            continue
        entry_start = t.entry_candle_start
        R_pct = t.R / abs(t.planned_entry) if t.planned_entry else 0.0
        others = [x for x in tokens if x != t.token and entry_start in index.get(x, {})]
        for _ in range(n_rand):
            if not others:
                break
            tok = rng.choice(others)
            cs = day_candles[tok]
            i = index[tok][entry_start]
            inst = engine.universe.instruments[tok]
            params = engine.params_for(inst)
            ve = view(cs[i], t.direction)
            stop = ve.close - R_pct * abs(ve.close)
            rt = simulate_trade(cfg, inst, t.direction, cs[i], cs[i + 1:], stop, params, session,
                                max(cs[i].turnover_cr, 1e-6), "RND")
            if rt is not None and rt.status == "closed":
                rnd_out.append(TradeRecord.from_trade(rt, None, source="random"))
    return s1_out, rnd_out


def _atr_before(cs: list[Candle], i: int, params: Params, baseline: Baseline | None) -> float | None:
    from ..indicators import WilderATR

    a = WilderATR(params.atr_period, baseline.atr_seed if baseline else None)
    for c in cs[:i]:
        a.update(c.high, c.low, c.close)
    return a.current


# --------------------------------------------------------------------------- tick backtest
class TickBacktest:
    """Recorded days (raw snapshots + clock events) through the full pipeline."""

    def __init__(self, cfg: AppConfig, data_dir: str | Path, calendar: TradingCalendar | None = None,
                 events: EventCalendar | None = None, compare: bool = True):
        self.cfg = cfg
        self.data_dir = Path(data_dir)
        self.calendar = calendar
        self.events = events
        self.compare = compare

    def available_days(self) -> list[date]:
        root = self.data_dir / "raw"
        out = []
        for p in sorted(root.glob("*/universe.json")) if root.exists() else []:
            try:
                out.append(date.fromisoformat(p.parent.name))
            except ValueError:
                continue
        return out

    def run(self, start: date | None = None, end: date | None = None) -> BacktestResult:
        from ..daily import load_day_reference
        from ..pipeline import Processor, replay
        from ..storage.raw import raw_dir, read_raw

        days = []
        for d in self.available_days():
            if (start and d < start) or (end and d > end):
                continue
            universe, baselines = load_day_reference(self.data_dir, d)
            records = read_raw(raw_dir(self.data_dir, d))
            if not records:
                continue
            day = DayInfo.build(d, self.cfg.session, self.calendar, self.events)
            proc = Processor(self.cfg, universe, baselines, day)
            events = replay(proc, records, end_ts=records[-1].recv_ts + 120.0, finish=True)
            eng = proc.engine
            trades = [TradeRecord.from_trade(t, eng.setups.get(t.setup_id))
                      for t in eng.trades.trades.values() if t.status == "closed"]
            res = DayResult(date=d, events=events, trades=trades)
            if self.compare:
                day_candles = {tok: list(tr.candles) for tok, tr in eng.trackers.items()}
                res.s1_trades, res.random_trades = compare_entries(self.cfg, eng, day_candles, day.session, d,
                                                                   self.cfg.backtest.random_seed)
            days.append(res)
        return BacktestResult(days=days, config_version=self.cfg.version(), mode="ticks")


def with_params(cfg: AppConfig, **params) -> AppConfig:
    data = cfg.model_dump()
    data["params"].update(params)
    return AppConfig.model_validate(data)


def run_days(bt: CandleBacktest, days: Iterable[date]) -> BacktestResult:
    return BacktestResult(days=[bt.run_day(d) for d in days], config_version=bt.cfg.version(), mode="candles")
