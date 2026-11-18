"""Daily start-up routine for live mode (spec §4, §8.2, §8.4):
instrument master → F&O universe → ban list → baselines (20 sessions of
1-minute history) → price bands → universe with liquidity floor."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import date, datetime, time as dtime
from pathlib import Path

from .baseline import Baseline, BaselineStore, MinuteBar, build_baseline, typical_turnover_cr
from .calendar import EventCalendar, SessionRules, TradingCalendar
from .config import AppConfig, Params
from .instruments import Universe, build_universe, load_ban_list, parse_instrument_master
from .models import Instrument, Kind
from .storage.candles import CandleStore, sessions_from_store
from .timeutil import IST, ist_date, minute_of_day

log = logging.getLogger(__name__)


async def build_baselines(universe: Universe, today: date, calendar: TradingCalendar, params: Params,
                          session: SessionRules, rest=None, store: CandleStore | None = None,
                          futures_in_lots: bool = False) -> dict[int, Baseline]:
    """Same-minute profiles from stored candles when enough sessions exist,
    otherwise from the broker's historical 1-minute API (no look-ahead: only
    sessions before ``today``)."""
    days = calendar.previous_trading_days(today, params.baseline_sessions)
    out: dict[int, Baseline] = {}
    for n, inst in enumerate(sorted(universe.instruments.values(), key=lambda i: i.token)):
        minutes = range(session.cont_start, session.session_end(inst.kind))
        sessions: list[list[MinuteBar]] = []
        if store is not None:
            sessions = [s for s in sessions_from_store(store, inst.token, days) if s]
        if len(sessions) < params.baseline_min_sessions and rest is not None and days:
            try:
                sessions = await _history_sessions(rest, inst, days, futures_in_lots)
            except Exception as exc:
                log.warning("history for %s failed: %s", inst.display, exc)
        out[inst.token] = build_baseline(inst.token, sessions, minutes, params.baseline_smoothing_minutes,
                                         params.baseline_floor_cr, params.atr_period, asof=today)
        if n % 50 == 0:
            log.info("baselines: %d/%d", n, len(universe.instruments))
    return out


async def _history_sessions(rest, inst: Instrument, days: list[date], futures_in_lots: bool) -> list[list[MinuteBar]]:
    start = datetime.combine(days[0], dtime(9, 0), IST).replace(tzinfo=None)
    end = datetime.combine(days[-1], dtime(15, 45), IST).replace(tzinfo=None)
    rows = await rest.historical(inst.token, start, end, "minute", oi=inst.is_future)
    by_day: dict[date, list[MinuteBar]] = defaultdict(list)
    mult = inst.lot_size if (futures_in_lots and inst.is_future) else 1
    for r in rows:
        vol = int(r["volume"]) * mult
        by_day[ist_date(r["ts"])].append(MinuteBar(minute_of_day(r["ts"]), r["open"], r["high"], r["low"], r["close"],
                                                   vol, typical_turnover_cr(r["high"], r["low"], r["close"], vol),
                                                   r.get("oi")))
    return [by_day[d] for d in days if by_day.get(d)]


async def prepare_live_day(cfg: AppConfig, today: date, rest, store: CandleStore | None = None
                           ) -> tuple[Universe, dict[int, Baseline], EventCalendar, TradingCalendar]:
    params = cfg.base_params()
    calendar = TradingCalendar.from_config(cfg.calendar)
    events = EventCalendar.load(cfg.calendar.events_file)
    ref = Path(cfg.storage.data_dir) / "reference"
    ref.mkdir(parents=True, exist_ok=True)
    csv_text = await rest.instruments_csv()
    (ref / f"instruments-{today.isoformat()}.csv").write_text(csv_text, encoding="utf-8")
    rows = parse_instrument_master(csv_text)
    ban, src = await asyncio.to_thread(load_ban_list, cfg.universe)
    prelim = build_universe(rows, today, calendar, ban, params, cfg.universe)
    session = SessionRules(cfg.session, calendar.special_session(today))
    bstore = BaselineStore(cfg.storage.baseline_dir)
    baselines = bstore.load(today)
    if baselines is None:
        baselines = await build_baselines(prelim, today, calendar, params, session, rest, store,
                                          cfg.broker.kite.futures_volume_unit == "lots")
        bstore.save(today, baselines)
    bands = await rest.price_bands([i for i in prelim.instruments.values() if i.kind != Kind.IDX])
    universe = build_universe(rows, today, calendar, ban, params, cfg.universe,
                              adtv_cr={t: b.adtv_cr for t, b in baselines.items() if b.sessions > 0}, bands=bands)
    universe.notes.append(f"ban list source: {src}; {len(ban)} stocks in ban")
    return universe, baselines, events, calendar


def save_day_reference(data_dir: str | Path, universe: Universe, baselines: dict[int, Baseline]) -> Path:
    """Store the universe and baselines next to the raw ticks so the day can be replayed."""
    d = Path(data_dir) / "raw" / universe.date.isoformat()
    d.mkdir(parents=True, exist_ok=True)
    (d / "universe.json").write_text(json.dumps(universe.to_dict()), encoding="utf-8")
    (d / "baselines.json").write_text(json.dumps({str(k): v.to_dict() for k, v in baselines.items()}),
                                      encoding="utf-8")
    return d


def load_day_reference(data_dir: str | Path, day: date) -> tuple[Universe, dict[int, Baseline]]:
    d = Path(data_dir) / "raw" / day.isoformat()
    u = Universe.from_dict(json.loads((d / "universe.json").read_text(encoding="utf-8")))
    raw = json.loads((d / "baselines.json").read_text(encoding="utf-8"))
    return u, {int(k): Baseline.from_dict(v) for k, v in raw.items()}
