"""Baseline engine (spec §3, §8.4).

Pre-market job: for each instrument, the same-minute turnover profile B(m) =
median turnover of minute-of-day m over the last N sessions, pooled over m±k
minutes (the median resists one-off spikes); 20-day average daily turnover
(ADTV); and an ATR seed from the previous session.

Broker historical 1-minute candles carry no ATP, so their turnover is
approximated as volume x typical price (H+L+C)/3. Candles recorded by this
system carry exact turnover and are preferred once enough sessions exist.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .indicators import WilderATR
from .models import Candle


@dataclass(slots=True)
class MinuteBar:
    minute: int          # IST minute of day
    open: float
    high: float
    low: float
    close: float
    volume: int
    turnover_cr: float
    oi: int | None = None


def typical_turnover_cr(high: float, low: float, close: float, volume: float) -> float:
    return volume * (high + low + close) / 3.0 / 1e7


def bars_from_candles(candles: Iterable[Candle]) -> list[MinuteBar]:
    return [MinuteBar(c.minute, c.open, c.high, c.low, c.close, c.volume, c.turnover_cr, c.oi_close)
            for c in candles]


@dataclass
class Baseline:
    token: int
    profile: dict[int, float] = field(default_factory=dict)   # minute -> ₹ crore
    adtv_cr: float = 0.0
    atr_seed: float | None = None
    sessions: int = 0
    last_close: float | None = None
    asof: date | None = None
    floor_cr: float = 0.01

    def b(self, minute: int) -> float | None:
        v = self.profile.get(minute)
        if v is None:
            return None
        return v if v > self.floor_cr else self.floor_cr

    def to_dict(self) -> dict:
        return {"token": self.token, "profile": {str(k): round(v, 6) for k, v in self.profile.items()},
                "adtv_cr": self.adtv_cr, "atr_seed": self.atr_seed, "sessions": self.sessions,
                "last_close": self.last_close, "asof": self.asof.isoformat() if self.asof else None,
                "floor_cr": self.floor_cr}

    @classmethod
    def from_dict(cls, d: Mapping) -> "Baseline":
        return cls(token=int(d["token"]), profile={int(k): float(v) for k, v in d["profile"].items()},
                   adtv_cr=float(d.get("adtv_cr", 0.0)), atr_seed=d.get("atr_seed"),
                   sessions=int(d.get("sessions", 0)), last_close=d.get("last_close"),
                   asof=date.fromisoformat(d["asof"]) if d.get("asof") else None,
                   floor_cr=float(d.get("floor_cr", 0.01)))

    @classmethod
    def flat(cls, token: int, b_cr: float, minutes: Iterable[int], adtv_cr: float = 100.0,
             atr_seed: float | None = None, sessions: int = 20) -> "Baseline":
        """A constant profile (tests, demos)."""
        return cls(token=token, profile={m: b_cr for m in minutes}, adtv_cr=adtv_cr, atr_seed=atr_seed,
                   sessions=sessions)


def build_baseline(token: int, sessions: Sequence[Sequence[MinuteBar]], minutes: Sequence[int],
                   smoothing: int = 2, floor_cr: float = 0.01, atr_period: int = 14,
                   asof: date | None = None) -> Baseline:
    """``sessions`` oldest first; each is that session's minute bars. Minutes in
    ``minutes`` without a bar count as zero turnover (no trades)."""
    sessions = [s for s in sessions if s]
    minute_list = list(minutes)
    mset = set(minute_list)
    per_session: list[dict[int, float]] = []
    daily: list[float] = []
    for bars in sessions:
        d = {b.minute: b.turnover_cr for b in bars if b.minute in mset}
        per_session.append(d)
        daily.append(sum(d.values()))

    profile: dict[int, float] = {}
    if per_session:
        lo, hi = min(minute_list), max(minute_list)
        for m in minute_list:
            pool: list[float] = []
            for k in range(m - smoothing, m + smoothing + 1):
                if k < lo or k > hi or k not in mset:
                    continue
                for d in per_session:
                    pool.append(d.get(k, 0.0))
            profile[m] = statistics.median(pool) if pool else 0.0

    atr_seed = None
    last_close = None
    if sessions:
        last = sessions[-1]
        a = WilderATR(atr_period)
        for b in last:
            a.update(b.high, b.low, b.close)
        atr_seed = a.current
        last_close = last[-1].close

    return Baseline(token=token, profile=profile, adtv_cr=(sum(daily) / len(daily)) if daily else 0.0,
                    atr_seed=atr_seed, sessions=len(sessions), last_close=last_close, asof=asof,
                    floor_cr=floor_cr)


class BaselineStore:
    """Baselines for one trading day as a JSON file under ``storage.baseline_dir``."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def path(self, d: date) -> Path:
        return self.dir / f"{d.isoformat()}.json"

    def save(self, d: date, baselines: Mapping[int, Baseline]) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        p = self.path(d)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({str(k): v.to_dict() for k, v in baselines.items()}), encoding="utf-8")
        tmp.replace(p)
        return p

    def load(self, d: date) -> dict[int, Baseline] | None:
        p = self.path(d)
        if not p.exists():
            return None
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {int(k): Baseline.from_dict(v) for k, v in raw.items()}
