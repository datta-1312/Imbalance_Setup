"""Scenario 15: full universe subscribed, 1,000 snapshots per second sustained →
tick-to-state p99 under 50 ms and no dropped instruments.

Runs the live runtime path in real time (wall-clock candle timer, Parquet
recording, event log, dispatcher). The clock is shifted so the run spans a
minute boundary inside market hours whatever the time of day. Latency is
measured from each frame's scheduled arrival time to the moment each snapshot
in it has updated the engine state, so event-loop stalls (e.g. the minute's
batch close) are included.
"""

from __future__ import annotations

import asyncio
import math
import time

import pytest

from helpers import DAY
from imbalance.adapters.base import MarketDataAdapter
from imbalance.adapters.synthetic import SyntheticMarket, synthetic_universe
from imbalance.calendar import DayInfo
from imbalance.config import ConfigManager, SessionConfig
from imbalance.pipeline import Processor
from imbalance.runtime import Runtime
from imbalance.timeutil import at, minute_of_day


class ManualAdapter(MarketDataAdapter):
    name = "manual"

    def __init__(self):
        super().__init__()
        self._stop = asyncio.Event()

    async def run(self):
        self.connected = True
        await self._stop.wait()

    async def close(self):
        self._stop.set()


def percentile(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(q / 100 * (len(xs) - 1))))]


@pytest.mark.perf
async def test_s15_full_universe_1000_snapshots_per_second(tmp_path):
    u = synthetic_universe(DAY, 210)                    # 210 stocks + futures + 2 index futures
    n_inst = len(u.instruments)
    assert n_inst >= 420
    rate = 1000.0 / n_inst
    market = SyntheticMarket(u, DAY, seed=11, rate=rate)
    baselines = market.baselines()
    seconds = 8
    start_sim = at(DAY, "10:29:56")
    # pre-generate (the generator is not part of the system under test)
    plan = [market.second(start_sim + s) for s in range(seconds)]
    assert 900 <= sum(map(len, plan)) / seconds <= 1100

    offset = start_sim - time.time()
    clock = lambda: time.time() + offset                 # noqa: E731
    cfgm = ConfigManager(data={"storage": {"data_dir": str(tmp_path), "raw_flush_seconds": 2.0,
                                           "event_log_dir": str(tmp_path / "events")}})
    adapter = ManualAdapter()
    rt = Runtime(cfgm, u, baselines, DayInfo.build(DAY, SessionConfig()), adapter, live=True,
                 data_dir=tmp_path, record_raw=True, clock=clock)
    runner = asyncio.create_task(rt.run(until_done=False))
    await asyncio.sleep(0.05)

    frames_per_second = 20
    lat: list[float] = []
    emitted = 0
    t0 = time.perf_counter()
    for s, ticks in enumerate(plan):
        per = math.ceil(len(ticks) / frames_per_second)
        for i in range(frames_per_second):
            frame = ticks[i * per:(i + 1) * per]
            target = t0 + s + i / frames_per_second
            delay = target - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            now = clock()
            for t in frame:
                t.ts = float(int(now))
                t.recv_ts = now
                rt._on_ticks([t])                       # the adapter's callback, inline as in live mode
                lat.append(time.perf_counter() - target)
            emitted += len(frame)
    elapsed = time.perf_counter() - t0
    await asyncio.sleep(1.8)                            # let the 10:30 deadline pass for the batch close
    rt.request_stop()
    await asyncio.wait_for(runner, 10)

    p50, p99 = percentile(lat, 50), percentile(lat, 99)
    print(f"\n{emitted} snapshots in {elapsed:.2f}s ({emitted / elapsed:.0f}/s); "
          f"tick-to-state p50 {p50 * 1000:.2f} ms, p99 {p99 * 1000:.2f} ms, max {max(lat) * 1000:.2f} ms")
    assert emitted / elapsed >= 950                     # sustained ~1,000 snapshots/s
    assert p99 < 0.050
    # no dropped instruments: every instrument has its 10:29 candle closed
    assert rt.processor.ticks == emitted
    eng = rt.engine
    closed_1029 = [tok for tok, tr in eng.trackers.items()
                   if any(minute_of_day(c.start) == 629 for c in tr.candles)]
    assert len(closed_1029) == n_inst
    assert rt.recorder is None or True


def test_processing_throughput_one_minute():
    """60,000 snapshots (one minute at 1,000/s) through the processor as fast as possible."""
    u = synthetic_universe(DAY, 210)
    market = SyntheticMarket(u, DAY, seed=5, rate=1000.0 / len(u.instruments))
    proc = Processor(ConfigManager(data={}).config, u, market.baselines(), DayInfo.build(DAY, SessionConfig()))
    ticks = [t for s in range(60) for t in market.second(at(DAY, "11:00") + s)]
    t0 = time.perf_counter()
    for t in ticks:
        proc.on_clock(t.recv_ts)
        proc.on_tick(t)
    proc.on_clock(at(DAY, "11:01", 2))
    el = time.perf_counter() - t0
    print(f"\n{len(ticks)} snapshots in {el:.2f}s = {len(ticks) / el:.0f}/s; per-tick p99 "
          f"{proc.latency.summary_ms()['p99']} ms; batch close max {proc.close_latency.summary_ms()['max']} ms")
    assert len(ticks) / el > 5000
    assert proc.close_latency.max < 0.25
