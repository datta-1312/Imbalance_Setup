"""Runtime: wires adapter -> processor -> (event log, recorder, candle store,
alert dispatcher, bus) and runs the wall-clock candle timer in live mode.

Ticks are processed inline in the adapter's callback (no queue hop) so the
tick-to-state latency is just the processing time; slow work (Parquet, the
database, Telegram, browsers) happens off the hot path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

from .adapters.base import MarketDataAdapter
from .alerts import AlertDispatcher, TelegramSender
from .baseline import Baseline
from .bus import InMemoryBus
from .calendar import DayInfo
from .config import ConfigManager
from .engine import Event
from .instruments import Universe
from .models import Candle
from .pipeline import Processor
from .stats import LatencyStats, RateMeter
from .storage.candles import AsyncCandleWriter, CandleStore
from .storage.eventlog import EventLogWriter
from .storage.raw import RawTickRecorder
from .timeutil import minute_of_day, parse_hhmm

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, cfgm: ConfigManager, universe: Universe, baselines: dict[int, Baseline], day: DayInfo,
                 adapter: MarketDataAdapter, *, live: bool, data_dir: str | Path | None = None,
                 record_raw: bool | None = None, event_log: bool = True, candle_store: CandleStore | None = None,
                 bus: InMemoryBus | None = None, telegram: TelegramSender | None = None,
                 event_log_name: str | None = None, clock=time.time):
        cfg = cfgm.config
        self.clock = clock
        self.cfgm = cfgm
        self.universe = universe
        self.day = day
        self.live = live
        self.adapter = adapter
        self.bus = bus or InMemoryBus()
        self.processor = Processor(cfgm, universe, baselines, day, on_candles=self._on_candles)
        self.engine = self.processor.engine
        self.telegram = telegram
        self.dispatcher = AlertDispatcher(lambda: self.cfgm.config, self.bus, telegram, clock=clock,
                                          measure_latency=live)
        data_dir = Path(data_dir or cfg.storage.data_dir)
        self.data_dir = data_dir
        record = cfg.storage.record_raw_ticks if record_raw is None else record_raw
        self.recorder = RawTickRecorder(data_dir, day.date, cfg.storage.raw_rotate_minutes,
                                        cfg.storage.raw_compression) if record else None
        ev_dir = Path(cfg.storage.event_log_dir) if live else data_dir / "events"
        self.eventlog = EventLogWriter(ev_dir, day.date, cfgm.version, name=event_log_name) if event_log else None
        symbols = {t: i.display for t, i in universe.instruments.items()}
        self.candle_writer = AsyncCandleWriter(candle_store, symbols) if candle_store is not None else None
        self.tick_rate = RateMeter()
        self.tick_latency = LatencyStats()      # frame arrival -> state updated, per tick
        self.handle_latency = LatencyStats()
        self.recent_events: deque[dict] = deque(maxlen=5000)
        self.sim_time: float | None = None
        self.started = time.time()
        self.finished = False
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        adapter.on_ticks = self._on_ticks
        adapter.on_status = self._on_status
        self.drives_clock = bool(getattr(adapter, "drives_clock", False))
        if self.drives_clock:
            adapter.on_time = self._on_time
        cfgm.on_change(self._config_changed)

    # ------------------------------------------------------------------ time
    def now(self) -> float:
        if self.drives_clock and self.sim_time is not None:
            return self.sim_time
        return self.clock()

    # ------------------------------------------------------------------ hot path
    def _on_ticks(self, ticks: list) -> None:
        t0 = time.perf_counter()
        rec = self.recorder
        proc = self.processor
        lat = self.tick_latency
        for t in ticks:
            if rec is not None:
                rec.add(t)
            ev = proc.on_tick(t)
            if ev:
                self._handle(ev)
            lat.add(time.perf_counter() - t0)
        self.tick_rate.add(self.now(), len(ticks))

    def _on_time(self, t: float) -> None:
        self.sim_time = t
        if self.recorder is not None:
            self.recorder.add_clock(t)
        ev = self.processor.on_clock(t)
        if ev:
            self._handle(ev)

    def _on_status(self, status: str, ts: float, info: dict) -> None:
        kind = {"disconnected": "disconnect", "reconnected": "reconnect"}.get(status)
        events: list[Event] = []
        if kind is not None:
            if self.recorder is not None:
                self.recorder.add_feed_event(kind, ts, ts)
            events += self.processor.on_feed_event(kind, ts)
        if status in ("disconnected", "reconnected", "error"):
            events += self.engine.system_event(ts, f"feed_{status}", {"message": str(info or "")},
                                               alert=status != "reconnected")
        if events:
            self._handle(events)

    def _on_candles(self, candles: list[Candle]) -> None:
        if self.candle_writer is not None:
            self.candle_writer.submit(candles)

    def _handle(self, events: list[Event]) -> None:
        t0 = time.perf_counter()
        if self.eventlog is not None:
            self.eventlog.write(events)
        self.dispatcher.submit(events)
        recs = [e.to_record() for e in events]
        self.recent_events.extend(recs)
        self.bus.publish("events", recs)
        self.handle_latency.add(time.perf_counter() - t0)

    def _config_changed(self, cfg) -> None:
        ev = self.engine.system_event(self.now(), "config_reloaded", {"version": self.cfgm.version}, alert=False)
        if self.eventlog is not None:
            self.eventlog.config_version = self.cfgm.version
        self._handle(ev)

    # ------------------------------------------------------------------ loops
    async def _clock_loop(self) -> None:
        shutdown = parse_hhmm(self.cfgm.config.session.shutdown_time)
        while not self._stop.is_set():
            nd = self.processor.builder.next_deadline()
            now = self.clock()
            wait = 1.0 if nd is None else min(1.0, max(0.0, nd - now))
            await asyncio.sleep(wait)
            now = self.clock()
            if self.recorder is not None:
                self.recorder.add_clock(now)
            ev = self.processor.on_clock(now)
            if ev:
                self._handle(ev)
            if minute_of_day(now) >= shutdown or self.day.date < _today(now):
                await self.finish(now)
                self.request_stop()
                return

    async def _snapshot_loop(self) -> None:
        interval = self.cfgm.config.api.snapshot_interval_seconds
        while not self._stop.is_set():
            await asyncio.sleep(interval)
            if self.bus.subscribers:
                self.bus.publish("snapshot", self.snapshot())

    async def finish(self, ts: float | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        ts = ts if ts is not None else self.now() + 120.0
        ev = self.processor.finish(ts)
        if ev:
            self._handle(ev)
        self.bus.publish("snapshot", self.snapshot())

    async def run(self, until_done: bool = True) -> None:
        cfg = self.cfgm.config
        tasks = [asyncio.create_task(self.adapter.run(), name="adapter"),
                 asyncio.create_task(self._snapshot_loop(), name="snapshots")]
        if not self.drives_clock:
            tasks.append(asyncio.create_task(self._clock_loop(), name="clock"))
        if self.recorder is not None:
            tasks.append(asyncio.create_task(self.recorder.flush_loop(cfg.storage.raw_flush_seconds), name="raw"))
        if self.telegram is not None:
            tasks.append(asyncio.create_task(self.telegram.run(), name="telegram"))
        if self.candle_writer is not None:
            tasks.append(asyncio.create_task(self.candle_writer.run(), name="candles"))
        if self.live:
            tasks.append(asyncio.create_task(self.cfgm.watch(), name="config-watch"))
        self._tasks = tasks
        try:
            if self.drives_clock and until_done:
                await tasks[0]
                await self.finish(None if self.sim_time is None else self.sim_time + 120.0)
            else:
                await self._stop.wait()
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._stop.set()
        try:
            await self.adapter.close()
        except Exception:  # pragma: no cover
            log.exception("adapter close failed")
        for t in self._tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        if self.recorder is not None:
            await asyncio.to_thread(self.recorder.close)
        if self.eventlog is not None:
            self.eventlog.close()
            self.eventlog = None

    def request_stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------ views
    def health(self) -> dict[str, Any]:
        now = self.now()
        p = self.processor.health()
        return {
            "mode": "live" if self.live else ("simulated" if self.drives_clock else "replay"),
            "now": now,
            "date": self.day.date.isoformat(),
            "feed": self.adapter.stats(),
            "ticks_per_second": round(self.tick_rate.rate(now), 1),
            "tick_to_state_ms": self.tick_latency.summary_ms(),
            "processor": p,
            "alerts": self.dispatcher.stats(),
            "handle_latency_ms": self.handle_latency.summary_ms(),
            "bus": {"subscribers": self.bus.subscribers, "published": self.bus.published,
                    "dropped": self.bus.dropped},
            "recorder_rows": self.recorder.rows if self.recorder else None,
            "candles_written": self.candle_writer.written if self.candle_writer else None,
            "config_version": self.cfgm.version,
            "universe": {"instruments": len(self.universe.instruments),
                         "scannable": sum(1 for i in self.universe.instruments.values() if i.scannable),
                         "banned": sorted(self.universe.banned), "notes": self.universe.notes[:20]},
        }

    def snapshot(self) -> dict[str, Any]:
        snap = self.engine.snapshot(self.now())
        snap["health"] = self.health()
        snap["alerts"] = list(self.dispatcher.recent)[-60:]
        return snap


def _today(ts: float):
    from .timeutil import ist_date

    return ist_date(ts)
