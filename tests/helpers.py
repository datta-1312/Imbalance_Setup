"""Shared builders for scenario tests."""

from __future__ import annotations

from datetime import date
from typing import Iterable

from imbalance.baseline import Baseline
from imbalance.calendar import DayInfo, EventCalendar
from imbalance.config import AppConfig, SessionConfig
from imbalance.engine import Event
from imbalance.instruments import Universe
from imbalance.models import Instrument, Kind
from imbalance.pipeline import Processor, replay
from imbalance.scenario import TickScript, merge_records
from imbalance.timeutil import at

DAY = date(2026, 11, 18)
ALL_MINUTES = range(555, 940)
EQ_TOKEN = 1001
FUT_TOKEN = 2001


def eq(token: int = EQ_TOKEN, und: str = "ABC", **kw) -> Instrument:
    kw.setdefault("tick_size", 0.01)
    return Instrument(token=token, symbol=und, underlying=und, kind=Kind.EQ, **kw)


def fut(token: int = FUT_TOKEN, und: str = "ABC", lot: int = 500, **kw) -> Instrument:
    kw.setdefault("tick_size", 0.01)
    return Instrument(token=token, symbol=f"{und}26NOVFUT", underlying=und, kind=Kind.FUT, exchange="NFO",
                      lot_size=lot, expiry=date(2026, 11, 24), series="current", **kw)


def link(a: Instrument, b: Instrument) -> None:
    a.linked_token, b.linked_token = b.token, a.token


def universe(*insts: Instrument) -> Universe:
    return Universe(date=DAY, instruments={i.token: i for i in insts})


def baselines(insts: Iterable[Instrument], b_cr: float = 0.625, overrides: dict[int, float] | None = None,
              atr_seed: float = 0.2) -> dict[int, Baseline]:
    out = {}
    for i in insts:
        b = Baseline.flat(i.token, b_cr, ALL_MINUTES, atr_seed=atr_seed)
        for m, v in (overrides or {}).items():
            b.profile[m] = v
        out[i.token] = b
    return out


def config(**params) -> AppConfig:
    return AppConfig.from_dict({"params": params})


def processor(insts: list[Instrument], cfg: AppConfig | None = None, b_cr: float = 0.625,
              b_overrides: dict[int, float] | None = None, events: EventCalendar | None = None,
              mode: str = "ticks") -> Processor:
    cfg = cfg or config()
    day = DayInfo.build(DAY, SessionConfig(), events=events)
    return Processor(cfg, universe(*insts), baselines(insts, b_cr, b_overrides), day, mode=mode)


def t(hhmmss: str) -> float:
    """'10:28:05' -> epoch on the test day."""
    parts = hhmmss.split(":")
    return at(DAY, f"{parts[0]}:{parts[1]}", float(parts[2]) if len(parts) > 2 else 0.0)


def run(proc: Processor, *scripts: TickScript, end: str = "15:45", finish: bool = False) -> list[Event]:
    """Replay merged scripts; the clock then advances to ``end`` ("HH:MM[:SS]")."""
    return replay(proc, merge_records(*scripts), end_ts=t(end), finish=finish)


def script(token: int = EQ_TOKEN, price: float = 100.0, oi: int | None = None, until: str = "10:21",
           turnover: float = 0.5) -> TickScript:
    """Pre-open snapshot plus quiet candles from 09:15 up to (not including) ``until``."""
    from imbalance.timeutil import parse_hhmm

    s = TickScript(token, DAY, price, tick_size=0.01, oi=oi)
    s.preopen()
    n = parse_hhmm(until) - 555
    if n > 0:
        s.quiet("09:15", n, turnover_cr=turnover)
    return s


def surge1(s: TickScript, hhmm: str = "10:21", turnover: float = 5.0, buy_share: float = 0.7,
           oi_change_pct: float = 0.0) -> TickScript:
    """The standard S1 candle: O 100.00 H 100.65 L 99.95 C 100.60."""
    return s.candle(hhmm, 100.0, 100.65, 99.95, 100.60, turnover, buy_share=buy_share, oi_change_pct=oi_change_pct)


PULLBACK = [  # (O, H, L, C) six lower-high candles, L reaches 100.37 (40% of 99.95 -> 100.65)
    (100.60, 100.62, 100.50, 100.52),
    (100.52, 100.55, 100.45, 100.47),
    (100.47, 100.50, 100.40, 100.42),
    (100.42, 100.45, 100.37, 100.40),
    (100.40, 100.43, 100.38, 100.41),
    (100.41, 100.44, 100.39, 100.42),
]


def pullback(s: TickScript, start: str = "10:22", candles=PULLBACK, turnover: float = 1.0,
             buy_share: float = 0.45) -> TickScript:
    from imbalance.timeutil import fmt_minute, parse_hhmm

    m0 = parse_hhmm(start)
    for k, (o, h, l, c) in enumerate(candles):
        s.candle(fmt_minute(m0 + k), o, h, l, c, turnover, buy_share=buy_share)
    return s


def by(events: list[Event], **kw) -> list[Event]:
    out = []
    for e in events:
        if all(getattr(e, k) == v for k, v in kw.items()):
            out.append(e)
    return out
