"""Alert dispatcher (spec §8.7): de-duplicates, applies mute and grade filters,
pushes to the dashboard over the bus and to Telegram, and measures latency."""

from __future__ import annotations

import time
from collections import Counter, deque
from typing import Callable

from ..config import AppConfig
from ..engine import Event
from ..stats import LatencyStats
from ..timeutil import fmt_hms, minute_start
from .formatting import SOUNDS, display_time, format_alert
from .telegram import TelegramSender

GRADE_RANK = {"A": 3, "B": 2, "C": 1}
CONFIRMED = {"S1_CONFIRMED", "ARMED", "ENTRY", "INVALIDATED", "EXPIRED"}


class AlertDispatcher:
    def __init__(self, config: Callable[[], AppConfig], bus, telegram: TelegramSender | None = None,
                 clock: Callable[[], float] = time.time, measure_latency: bool = True):
        self._config = config
        self.bus = bus
        self.telegram = telegram
        self.clock = clock
        self.measure_latency = measure_latency
        self.muted: set[int] = set()
        self.recent: deque[dict] = deque(maxlen=300)
        self._seen: dict[tuple, float] = {}
        self.counts: Counter = Counter()
        self.latency_confirmed = LatencyStats()
        self.latency_provisional = LatencyStats()

    def submit(self, events: list[Event]) -> list[dict]:
        out = []
        for e in events:
            if e.alert:
                rec = self._dispatch(e)
                if rec is not None:
                    out.append(rec)
        return out

    def _key(self, e: Event) -> tuple:
        bucket = e.values.get("candle") or minute_start(e.ts)
        return (e.token, e.alert, e.setup_id or "", e.values.get("trade_id") or "", e.reason or "", bucket)

    def _dispatch(self, e: Event) -> dict | None:
        cfg = self._config()
        window = cfg.alerts.dedupe_window_seconds
        key = self._key(e)
        last = self._seen.get(key)
        if last is not None and e.ts - last < window:
            self.counts["deduped"] += 1
            return None
        self._seen[key] = e.ts
        if len(self._seen) > 5000:
            cutoff = e.ts - window
            self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
        now = self.clock()
        latency_ms = None
        if self.measure_latency:
            latency_ms = round((now - e.ts) * 1000.0, 1)
            (self.latency_provisional if e.provisional else self.latency_confirmed).add(max(0.0, now - e.ts))
        muted = e.token in self.muted
        text = format_alert(e)
        rec = {
            "seq": e.seq, "type": e.alert, "kind": e.kind, "reason": e.reason, "ts": e.ts, "time": display_time(e),
            "token": e.token, "display": e.display, "symbol": e.symbol, "direction": e.direction,
            "setup_id": e.setup_id, "grade": e.grade, "score": e.score, "provisional": e.provisional,
            "text": text, "sound": None if (muted or not cfg.alerts.sounds) else SOUNDS.get(e.alert),
            "muted": muted, "latency_ms": latency_ms, "dispatched_at": fmt_hms(now),
        }
        self.recent.append(rec)
        self.counts[e.alert] += 1
        if self.bus is not None:
            self.bus.publish("alert", rec)
        if self.telegram is not None and not muted and self._telegram_wants(e, cfg):
            self.telegram.enqueue(text)
            self.counts["telegram"] += 1
        return rec

    @staticmethod
    def _telegram_wants(e: Event, cfg: AppConfig) -> bool:
        t = cfg.alerts.telegram
        if not t.enabled or e.alert not in t.alert_types:
            return False
        if e.grade and GRADE_RANK.get(e.grade, 0) < GRADE_RANK.get(t.min_grade, 0):
            return False
        return True

    def stats(self) -> dict:
        return {"counts": dict(self.counts), "muted": sorted(self.muted),
                "latency_confirmed_ms": self.latency_confirmed.summary_ms(),
                "latency_provisional_ms": self.latency_provisional.summary_ms(),
                "telegram": None if self.telegram is None else {"sent": self.telegram.sent,
                                                                "failed": self.telegram.failed,
                                                                "pending": self.telegram.pending}}
