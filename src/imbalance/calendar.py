"""Trading calendar, event calendar and per-day session rules (spec §4).

Every time comes from configuration: the exchange changed its timings several
times in 2025–26 (closing auction from 3 Aug 2026, futures until 15:40, futures
pre-open), so nothing here is hard-coded beyond the config defaults.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Iterable

from .config import CalendarConfig, Params, SessionConfig, SpecialSession
from .models import Kind
from .timeutil import parse_hhmm

log = logging.getLogger(__name__)

EVENT_DAY_TYPES = {"results", "board_meeting", "ex_date"}
INDEX_EVENT_TYPES = {"index_rebalance", "fo_inclusion", "fo_exclusion"}
HALT_TYPES = {"halt"}
ALL_EVENT_TYPES = EVENT_DAY_TYPES | INDEX_EVENT_TYPES | HALT_TYPES | {"expiry"}


class TradingCalendar:
    def __init__(self, holidays: Iterable[date] = (), weekend_days: Iterable[int] = (5, 6),
                 special_sessions: Iterable[SpecialSession] = ()):
        self.holidays = set(holidays)
        self.weekend_days = set(weekend_days)
        self.special = {s.date: s for s in special_sessions}

    @classmethod
    def from_config(cls, cfg: CalendarConfig) -> "TradingCalendar":
        holidays = set(cfg.holidays)
        if cfg.holidays_file:
            p = Path(cfg.holidays_file)
            if p.exists():
                holidays |= set(_read_dates(p))
        return cls(holidays, cfg.weekend_days, cfg.special_sessions)

    def is_trading_day(self, d: date) -> bool:
        """A normal full session (special sessions such as Muhurat are not)."""
        return d.weekday() not in self.weekend_days and d not in self.holidays and d not in self.special

    def special_session(self, d: date) -> SpecialSession | None:
        return self.special.get(d)

    def previous_trading_days(self, d: date, n: int) -> list[date]:
        """The ``n`` normal sessions before ``d`` (oldest first)."""
        out: list[date] = []
        cur = d - timedelta(days=1)
        guard = 0
        while len(out) < n and guard < 3660:
            if self.is_trading_day(cur):
                out.append(cur)
            cur -= timedelta(days=1)
            guard += 1
        return list(reversed(out))

    def next_trading_day(self, d: date) -> date:
        cur = d + timedelta(days=1)
        while not self.is_trading_day(cur):
            cur += timedelta(days=1)
        return cur

    def trading_days_between(self, a: date, b: date) -> list[date]:
        """Normal sessions in [a, b] inclusive."""
        out = []
        cur = a
        while cur <= b:
            if self.is_trading_day(cur):
                out.append(cur)
            cur += timedelta(days=1)
        return out

    def sessions_to_expiry(self, today: date, expiry: date) -> int:
        """Sessions from today to expiry, both inclusive (1 on expiry day)."""
        if expiry < today:
            return 0
        return len(self.trading_days_between(today, expiry))


def _read_dates(path: Path) -> list[date]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        token = line.split(",")[0].strip()
        if not token or token.startswith("#") or token.lower() == "date":
            continue
        try:
            out.append(date.fromisoformat(token))
        except ValueError:
            log.warning("holidays file %s: cannot parse %r", path, token)
    return out


@dataclass
class EventCalendar:
    """Corporate and index events: CSV rows ``date,symbol,type[,note]``.

    ``symbol`` may be ``*`` for a market-wide event (e.g. a halt).
    """

    events: dict[date, dict[str, set[str]]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path | None) -> "EventCalendar":
        cal = cls()
        if not path:
            return cal
        p = Path(path)
        if not p.exists():
            return cal
        with p.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                try:
                    d = date.fromisoformat(row["date"].strip())
                    sym = row["symbol"].strip().upper()
                    typ = row["type"].strip().lower()
                except (KeyError, ValueError, AttributeError):
                    log.warning("events file %s: bad row %r", p, row)
                    continue
                if typ not in ALL_EVENT_TYPES:
                    log.warning("events file %s: unknown event type %r", p, typ)
                cal.add(d, sym, typ)
        return cal

    def add(self, d: date, symbol: str, typ: str) -> None:
        self.events.setdefault(d, {}).setdefault(symbol.upper(), set()).add(typ)

    def tags(self, d: date, underlying: str) -> set[str]:
        day = self.events.get(d, {})
        return set(day.get(underlying.upper(), set())) | set(day.get("*", set()))

    def is_event_day(self, d: date, underlying: str) -> bool:
        return bool(self.tags(d, underlying) & EVENT_DAY_TYPES)

    def is_index_event(self, d: date, underlying: str) -> bool:
        return bool(self.tags(d, underlying) & INDEX_EVENT_TYPES)

    def is_halted(self, d: date, underlying: str) -> bool:
        return bool(self.tags(d, underlying) & HALT_TYPES)


class SessionRules:
    """Minute-of-day session windows for one trading day."""

    def __init__(self, cfg: SessionConfig, special: SpecialSession | None = None):
        self.cfg = cfg
        self.special = special
        if special is None:
            self.cont_start = parse_hhmm(cfg.continuous_start)
            self.cash_end = parse_hhmm(cfg.cash_continuous_end)
            self.fut_end = parse_hhmm(cfg.futures_end)
            self.eod_cash = parse_hhmm(cfg.eod_exit_cash)
            self.eod_fut = parse_hhmm(cfg.eod_exit_fut)
            self.block2 = (parse_hhmm(cfg.block_deal_2[0]), parse_hhmm(cfg.block_deal_2[1]))
        else:
            self.cont_start = parse_hhmm(special.start)
            self.cash_end = self.fut_end = parse_hhmm(special.end)
            self.eod_cash = self.eod_fut = max(self.cont_start, self.cash_end - 5)
            self.block2 = (0, 0)
        self.grace = cfg.candle_close_grace_seconds

    def session_end(self, kind: Kind) -> int:
        return self.cash_end if kind == Kind.EQ else self.fut_end

    def candle_allowed(self, kind: Kind, minute: int) -> bool:
        return self.cont_start <= minute < self.session_end(kind)

    def in_block_window(self, minute: int) -> bool:
        return self.block2[0] <= minute < self.block2[1]

    def eod_exit(self, kind: Kind) -> int:
        return self.eod_cash if kind == Kind.EQ else self.eod_fut

    # Signal windows use the (possibly per-symbol) tunable params.
    def surge1_allowed(self, minute: int, params: Params) -> bool:
        if self.special is not None and not params.scan_special_sessions:
            return False
        return params.no_signal_before_min <= minute < params.last_surge1_min

    def entry_allowed(self, minute: int, params: Params) -> bool:
        if self.special is not None and not params.scan_special_sessions:
            return False
        return params.no_signal_before_min <= minute < params.last_entry_min


@dataclass
class DayInfo:
    """Everything date-specific the engine needs."""

    date: date
    session: SessionRules
    events: EventCalendar = field(default_factory=EventCalendar)

    @classmethod
    def build(cls, d: date, session_cfg: SessionConfig, calendar: TradingCalendar | None = None,
              events: EventCalendar | None = None) -> "DayInfo":
        special = calendar.special_session(d) if calendar else None
        return cls(date=d, session=SessionRules(session_cfg, special), events=events or EventCalendar())
