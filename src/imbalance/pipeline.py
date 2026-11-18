"""Processor: feed snapshots, feed events and clock time in; engine events out.

The same Processor runs in live mode (driven by the websocket adapter and a
wall-clock timer) and in replay/backtests (driven by recorded receive times), so
backtests execute exactly the code that runs live (spec §8, §11).
"""

from __future__ import annotations

import time
from typing import Callable, Iterable

from .baseline import Baseline
from .calendar import DayInfo
from .candles import CandleBuilder, ClosedBatch
from .config import AppConfig, ConfigManager
from .engine import Event, ScreenerEngine
from .instruments import Universe
from .models import Candle, Tick
from .scenario import FeedEvent, Record, record_time
from .stats import LatencyStats


class Processor:
    def __init__(self, config: ConfigManager | AppConfig, universe: Universe, baselines: dict[int, Baseline],
                 day: DayInfo, mode: str = "ticks", on_candles: Callable[[list[Candle]], None] | None = None):
        cfg = config if isinstance(config, AppConfig) else config.config
        self.engine = ScreenerEngine(config, universe, baselines, day, mode=mode)
        params = cfg.base_params()
        self.builder = CandleBuilder(universe.instruments, day.session, stale_after_s=params.stale_after_seconds,
                                     futures_volume_in_lots=cfg.broker.kite.futures_volume_unit == "lots")
        self.day = day
        self.on_candles = on_candles
        self._last_stale_check = 0.0
        self.feed_connected = True
        self.latency = LatencyStats()          # tick -> state update (wall clock)
        self.close_latency = LatencyStats()    # candle deadline -> confirmed events (wall clock)
        self.ticks = 0
        self.last_tick_ts: float | None = None

    # ------------------------------------------------------------------ inputs
    def on_tick(self, t: Tick) -> list[Event]:
        t0 = time.perf_counter()
        self.ticks += 1
        self.last_tick_ts = t.ts
        forming, batches = self.builder.on_tick(t)
        events: list[Event] = []
        for b in batches:
            events += self._batch(b)
        status = self.builder.drain_status()
        for st in status:
            events += self.engine.on_status(st.token, st.status, st.ts)
        if t.token in self.engine.trades.active:
            events += self.engine.on_price(t.token, t.ltp, t.ts)
        if forming is not None and forming.start == self.builder.open_minute:
            events += self.engine.on_update(forming, t.ts)
        self.latency.add(time.perf_counter() - t0)
        return events

    def on_clock(self, now: float) -> list[Event]:
        events: list[Event] = []
        for b in self.builder.advance(now):
            t0 = time.perf_counter()
            events += self._batch(b)
            self.close_latency.add(time.perf_counter() - t0)
        if now - self._last_stale_check >= 1.0:
            self._last_stale_check = now
            self.builder.check_stale(now)
            for st in self.builder.drain_status():
                events += self.engine.on_status(st.token, st.status, st.ts)
        return events

    def on_feed_event(self, kind: str, ts: float) -> list[Event]:
        if kind == "disconnect":
            self.feed_connected = False
            self.builder.on_disconnect(ts)
        elif kind == "reconnect":
            self.feed_connected = True
            self.builder.on_reconnect(ts)
        return self.engine.system_event(ts, f"feed_{kind}", {}, alert=False)

    def finish(self, ts: float) -> list[Event]:
        """End of session: flush candles and close anything still open."""
        events = self.on_clock(ts)
        events += self.engine.end_of_day(ts)
        return events

    # ------------------------------------------------------------------ batches
    def _batch(self, b: ClosedBatch) -> list[Event]:
        close_ts = b.minute_start + 60.0 + self.builder.grace
        events = self.engine.on_batch(b.minute_start, b.candles, close_ts)
        if self.on_candles is not None:
            self.on_candles(list(b.candles.values()))
        # Snapshots of the next minute may have crossed a threshold inside the grace window.
        om = self.builder.open_minute
        for tok in sorted(b.candles):
            c = self.builder.forming(tok)
            if c is not None and c.n_ticks > 0 and c.start == om:
                events += self.engine.on_update(c, close_ts)
        return events

    # ------------------------------------------------------------------ views
    def health(self) -> dict:
        return {
            "feed_connected": self.feed_connected,
            "ticks": self.ticks,
            "last_tick_ts": self.last_tick_ts,
            "open_minute": self.builder.open_minute,
            "stale": [self.engine.trackers[t].inst.display for t in self.builder.stale_tokens()],
            "builder": dict(self.builder.counters),
            "tick_latency_ms": self.latency.summary_ms(),
            "close_latency_ms": self.close_latency.summary_ms(),
        }


def replay(processor: Processor, records: Iterable[Record], end_ts: float | None = None,
           finish: bool = False) -> list[Event]:
    """Drive a Processor from recorded data. The clock follows the recorded
    receive times, so candle closes happen where they happened live."""
    out: list[Event] = []
    last: float | None = None
    for r in records:
        t = record_time(r)
        out += processor.on_clock(t)
        if isinstance(r, FeedEvent):
            out += processor.on_feed_event(r.kind, r.ts)
        else:
            out += processor.on_tick(r)
        last = t
    if end_ts is None and last is not None:
        end_ts = last + 120.0
    if end_ts is not None:
        out += processor.finish(end_ts) if finish else processor.on_clock(end_ts)
    return out
