"""Engine vocabulary: states, reason codes, alert types, rule checks, events."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class State(StrEnum):
    IDLE = "IDLE"
    SURGE_1 = "SURGE_1"
    PULLBACK = "PULLBACK"
    ARMED = "ARMED"
    ENTRY = "ENTRY"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    COMPLETED = "COMPLETED"   # the trade opened at ENTRY has closed


LIVE_STATES = {State.SURGE_1, State.PULLBACK, State.ARMED}
END_STATES = {State.INVALIDATED, State.EXPIRED, State.COMPLETED}


class Reason(StrEnum):
    S1_CONFIRMED = "s1_confirmed"
    S1_CONTINUATION = "s1_continuation"
    S1_REJECTED_GRADE = "s1_rejected_grade"
    PULLBACK_STARTED = "pullback_started"
    NEW_HIGH = "new_high_resumed"
    ARMED = "armed"
    S2_ENTRY = "s2_entry"
    FLOOR_BROKEN = "floor_broken"
    MAX_RETRACE = "max_retrace"
    DISTRIBUTION = "distribution"
    SHORT_BUILDUP = "short_buildup"
    HALT = "halt"
    CIRCUIT = "circuit"
    STALE = "stale_data"
    RISK_OUT_OF_RANGE = "risk_out_of_range"
    TIMEOUT = "timeout"
    LAST_ENTRY_TIME = "last_entry_time"
    BUY_STOP_NOT_TRIGGERED = "buy_stop_not_triggered"
    TRADE_CLOSED = "trade_closed"
    PROVISIONAL = "provisional"
    PROVISIONAL_CANCELLED = "provisional_cancelled"
    CASH_FUT_LATE = "cash_fut_confirmed_late"


class AlertType(StrEnum):
    S1_PROVISIONAL = "S1_PROVISIONAL"
    S1_CONFIRMED = "S1_CONFIRMED"
    S1_CANCELLED = "S1_CANCELLED"
    ARMED = "ARMED"
    S2_PROVISIONAL = "S2_PROVISIONAL"
    S2_CANCELLED = "S2_CANCELLED"
    ENTRY = "ENTRY"
    T1 = "T1"
    EXIT = "EXIT"
    EXIT_WARNING = "EXIT_WARNING"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    SYSTEM = "SYSTEM"


def _clean(v: Any) -> Any:
    """JSON-safe, deterministic rounding of floats for the event log."""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, float):
        if math.isnan(v):
            return None
        if math.isinf(v):
            return "inf" if v > 0 else "-inf"
        return round(v, 6)
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, StrEnum):
        return v.value
    return v


@dataclass(slots=True)
class Check:
    rule: str
    value: Any
    threshold: Any
    passed: bool

    def as_dict(self) -> dict[str, Any]:
        return {"rule": self.rule, "value": _clean(self.value), "threshold": _clean(self.threshold),
                "pass": self.passed}


@dataclass(slots=True)
class Evaluation:
    passed: bool
    checks: list[Check]
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> list[str]:
        return [c.rule for c in self.checks if not c.passed]

    def checks_dicts(self) -> list[dict[str, Any]]:
        return [c.as_dict() for c in self.checks]


def evaluation(checks: list[Check], metrics: dict[str, Any] | None = None) -> Evaluation:
    return Evaluation(all(c.passed for c in checks), checks, metrics or {})


@dataclass(slots=True)
class Event:
    """One record of the append-only event log (and, if ``alert`` is set, an alert).

    ``ts`` is exchange/clock time, never wall-clock time, so replays are identical.
    """

    kind: str                      # transition | provisional | cancel | trade | status | update | system
    ts: float
    token: int = 0
    symbol: str = ""
    display: str = ""
    setup_id: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    reason: str | None = None
    alert: str | None = None
    provisional: bool = False
    grade: str | None = None
    score: float | None = None
    direction: int = 1
    values: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    def to_record(self) -> dict[str, Any]:
        from ..timeutil import fmt_hms, ist_date

        return {
            "seq": self.seq,
            "ts": _clean(self.ts),
            "date": ist_date(self.ts).isoformat() if self.ts else None,
            "time": fmt_hms(self.ts) if self.ts else None,
            "kind": self.kind,
            "token": self.token,
            "symbol": self.symbol,
            "display": self.display,
            "setup_id": self.setup_id,
            "from": self.from_state,
            "to": self.to_state,
            "reason": self.reason,
            "alert": self.alert,
            "provisional": self.provisional,
            "grade": self.grade,
            "score": _clean(self.score),
            "direction": self.direction,
            "values": _clean(self.values),
        }
