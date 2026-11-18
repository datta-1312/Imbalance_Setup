"""Exchange-time helpers.

All internal timestamps are POSIX epoch seconds (float). India Standard Time has
no daylight saving, so a fixed +05:30 offset is exact. Because the offset is a
whole number of minutes, minute boundaries are identical in UTC and IST.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
IST_OFFSET_S = 19800
DAY_S = 86400


def minute_start(ts: float) -> float:
    """Epoch of the start of the minute containing ``ts``."""
    return math.floor(ts / 60.0) * 60.0


def minute_of_day(ts: float) -> int:
    """IST minute of day (0..1439) for an epoch timestamp."""
    return int(((ts + IST_OFFSET_S) % DAY_S) // 60)


def ist_date(ts: float) -> date:
    return datetime.fromtimestamp(ts, IST).date()


def ist_datetime(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, IST)


@lru_cache(maxsize=4096)
def parse_hhmm(value: str) -> int:
    """'09:15' -> 555 (minutes after midnight). Seconds are not accepted."""
    parts = value.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {value!r}")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"invalid time {value!r}")
    return h * 60 + m


def fmt_minute(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def day_start(d: date) -> float:
    """Epoch of 00:00 IST on date ``d``."""
    return datetime.combine(d, time(0, 0), IST).timestamp()


def at(d: date, hhmm: str | int, seconds: float = 0.0) -> float:
    """Epoch for date ``d`` at IST clock time ``hhmm`` ("HH:MM", "HH:MM:SS" or a
    minute of day), plus ``seconds``."""
    if isinstance(hhmm, str):
        parts = hhmm.strip().split(":")
        m = parse_hhmm(":".join(parts[:2]))
        if len(parts) == 3:
            seconds += float(parts[2])
        elif len(parts) != 2:
            raise ValueError(f"expected HH:MM or HH:MM:SS, got {hhmm!r}")
    else:
        m = int(hhmm)
    return day_start(d) + m * 60 + seconds


def fmt_hms(ts: float | None) -> str:
    if ts is None:
        return "--:--:--"
    return ist_datetime(ts).strftime("%H:%M:%S")


def fmt_hm(ts: float | None) -> str:
    if ts is None:
        return "--:--"
    return ist_datetime(ts).strftime("%H:%M")
