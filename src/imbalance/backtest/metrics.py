"""Backtest metrics (spec §11): funnel, win rate, average R, expectancy, profit
factor, maximum drawdown, MAE/MFE — overall and split by grade, time of day,
EQ vs FUT, OI reading, event day and market regime."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..timeutil import minute_of_day


@dataclass
class TradeRecord:
    """A closed trade with the context needed for the splits."""

    id: str
    token: int
    display: str
    kind: str
    direction: int
    entry_ts: float
    closed_ts: float
    gross_r: float
    net_r: float
    net_pnl: float
    mae_r: float
    mfe_r: float
    exit_reason: str
    grade: str | None = None
    oi_reading: str | None = None
    event_day: bool = False
    regime: float | None = None
    shadow: bool = False
    tags: list[str] = field(default_factory=list)
    source: str = "s2"

    @classmethod
    def from_trade(cls, t, setup=None, regime: float | None = None, source: str = "s2") -> "TradeRecord":
        oi = None
        tags: list[str] = []
        if setup is not None:
            oi = ((setup.oi or {}).get("s2") or {}).get("reading") or ((setup.oi or {}).get("s1") or {}).get("reading")
            tags = list(setup.tags)
        R = t.R or 1.0
        return cls(id=t.id, token=t.token, display=t.display, kind=t.kind, direction=t.direction, entry_ts=t.entry_ts,
                   closed_ts=t.closed_ts or t.entry_ts, gross_r=t.gross_r or 0.0, net_r=t.net_r or 0.0,
                   net_pnl=t.net_pnl or 0.0, mae_r=t.mae / R, mfe_r=t.mfe / R, exit_reason=t.exit_reason or "",
                   grade=t.grade, oi_reading=oi, event_day="event day" in tags, regime=regime, shadow=t.shadow,
                   tags=tags, source=source)


def summarize(trades: Iterable[TradeRecord]) -> dict[str, Any]:
    ts = sorted(trades, key=lambda t: (t.closed_ts, t.id))
    n = len(ts)
    if n == 0:
        return {"trades": 0}
    wins = [t for t in ts if t.net_r > 0]
    losses = [t for t in ts if t.net_r <= 0]
    gross_win = sum(t.net_r for t in wins)
    gross_loss = -sum(t.net_r for t in losses)
    equity = 0.0
    peak = 0.0
    mdd = 0.0
    for t in ts:
        equity += t.net_r
        peak = max(peak, equity)
        mdd = max(mdd, peak - equity)
    return {
        "trades": n,
        "win_rate": round(len(wins) / n, 4),
        "avg_gross_r": round(sum(t.gross_r for t in ts) / n, 4),
        "expectancy_r": round(sum(t.net_r for t in ts) / n, 4),
        "total_net_r": round(sum(t.net_r for t in ts), 3),
        "avg_win_r": round(gross_win / len(wins), 4) if wins else None,
        "avg_loss_r": round(-gross_loss / len(losses), 4) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 3) if gross_loss > 0 else None,
        "max_drawdown_r": round(mdd, 3),
        "avg_mae_r": round(sum(t.mae_r for t in ts) / n, 3),
        "avg_mfe_r": round(sum(t.mfe_r for t in ts) / n, 3),
        "net_pnl": round(sum(t.net_pnl for t in ts), 2),
        "exits": _count(t.exit_reason for t in ts),
    }


def _count(xs: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for x in xs:
        out[x] += 1
    return dict(sorted(out.items()))


def time_bucket(ts: float) -> str:
    m = minute_of_day(ts)
    for end, label in ((630, "09:15-10:30"), (720, "10:30-12:00"), (810, "12:00-13:30"), (2000, "13:30-close")):
        if m < end:
            return label
    return "13:30-close"


def regime_bucket(x: float | None) -> str:
    if x is None:
        return "unknown"
    if x <= -0.5:
        return "NIFTY <= -0.5%"
    if x < 0:
        return "NIFTY -0.5..0%"
    if x < 0.5:
        return "NIFTY 0..+0.5%"
    return "NIFTY >= +0.5%"


SPLITS: dict[str, Callable[[TradeRecord], str]] = {
    "grade": lambda t: t.grade or "?",
    "time_of_day": lambda t: time_bucket(t.entry_ts),
    "instrument": lambda t: "EQ" if t.kind == "EQ" else "FUT",
    "oi_reading": lambda t: t.oi_reading or ("n/a" if t.kind == "EQ" else "unknown"),
    "event_day": lambda t: "event day" if t.event_day else "normal day",
    "regime": lambda t: regime_bucket(t.regime),
    "direction": lambda t: "long" if t.direction > 0 else "short",
}


def split_metrics(trades: list[TradeRecord]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for name, fn in SPLITS.items():
        groups: dict[str, list[TradeRecord]] = defaultdict(list)
        for t in trades:
            groups[fn(t)].append(t)
        out[name] = {k: summarize(v) for k, v in sorted(groups.items())}
    return out


def funnel(events: Iterable[Any]) -> dict[str, int]:
    """Surge 1 → armed → Surge 2 → entries (alerted) → winners, from the event log."""
    f = {"surge1": 0, "armed": 0, "surge2": 0, "entries": 0, "suppressed": 0, "risk_out_of_range": 0,
         "invalidated": 0, "expired": 0}
    for e in events:
        rec = e if isinstance(e, dict) else e.to_record()
        if rec["kind"] != "transition":
            continue
        to, reason = rec.get("to"), rec.get("reason")
        if to == "SURGE_1" and reason == "s1_confirmed":
            f["surge1"] += 1
        elif to == "ARMED":
            f["armed"] += 1
        elif to == "ENTRY":
            f["surge2"] += 1
            if (rec.get("values") or {}).get("suppressed"):
                f["suppressed"] += 1
            else:
                f["entries"] += 1
        elif to == "INVALIDATED":
            if reason == "risk_out_of_range":
                f["surge2"] += 1
                f["risk_out_of_range"] += 1
            else:
                f["invalidated"] += 1
        elif to == "EXPIRED":
            f["expired"] += 1
    return f
