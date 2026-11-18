"""Storage round trips, scenario 13 end to end, runtime wiring, dispatcher."""

from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from helpers import DAY, EQ_TOKEN, PULLBACK, by, config, eq, processor, pullback, run, script, surge1
from imbalance.adapters.synthetic import SimulatedFeed, SyntheticMarket, group_by_second, synthetic_universe
from imbalance.alerts import AlertDispatcher, TelegramSender, format_alert
from imbalance.bus import InMemoryBus
from imbalance.calendar import DayInfo
from imbalance.config import ConfigManager, SessionConfig
from imbalance.engine import Event
from imbalance.models import Flag, Tick
from imbalance.pipeline import replay
from imbalance.runtime import Runtime
from imbalance.scenario import FeedEvent, merge_records
from imbalance.storage.candles import SQLiteCandleStore, sessions_from_store
from imbalance.storage.eventlog import EventLogWriter, event_line, read_event_log
from imbalance.storage.raw import RawTickRecorder, read_raw
from imbalance.timeutil import at


def gap_script():
    s = script()
    s.feed_event("disconnect", "10:21", 20.5)
    s.candle("10:21", 100.0, 100.65, 99.95, 100.60, 5.0, buy_share=0.7, skip=(20, 40))
    s.feed_event("reconnect", "10:21", 40.5)
    s.quiet("10:22", 2)
    return s


# --------------------------------------------------------------------------- 13
def test_s13_feed_gap_candle_cannot_trigger_and_turnover_correct():
    proc = processor([eq()])
    s = gap_script()
    candles = []
    proc.on_candles = candles.extend
    events = run(proc, s, end="10:24:05")
    assert not by(events, alert="S1_CONFIRMED") and not by(events, alert="S1_PROVISIONAL")
    gap = [c for c in candles if c.flags & Flag.GAP]
    assert [c.minute for c in gap] == [621]
    assert gap[0].turnover_cr == pytest.approx(5.0, rel=0.01)
    ticks = [r for r in s.records if isinstance(r, Tick)]
    total = (ticks[-1].atp * ticks[-1].volume - ticks[0].atp * ticks[0].volume) / 1e7
    assert sum(c.turnover_cr for c in candles) == pytest.approx(total, rel=1e-6)
    assert [e.reason for e in by(events, kind="system")] == ["feed_disconnect", "feed_reconnect"]
    # control: the same candle without the gap does trigger
    s2 = script()
    surge1(s2)
    assert by(run(processor([eq()]), s2, end="10:22:05"), alert="S1_CONFIRMED")


def test_raw_parquet_roundtrip_and_identical_replay(tmp_path):
    s = surge1(script())
    s.feed_event("disconnect", "10:22", 20.5)
    s.candle("10:22", 100.60, 100.62, 100.50, 100.52, 1.0, skip=(20, 40))
    s.feed_event("reconnect", "10:22", 40.5)
    pullback(s, start="10:23", candles=PULLBACK[1:])
    ordered = merge_records(s)                                 # receive-time order, as recorded live
    rec = RawTickRecorder(tmp_path, DAY, rotate_minutes=1)
    half = len(ordered) // 2
    for i, r in enumerate(ordered):
        if isinstance(r, FeedEvent):
            rec.add_feed_event(r.kind, r.ts, r.recv_ts)
        else:
            rec.add(r)
        if i == half:
            rec.flush()
    rec.close()
    loaded = read_raw(tmp_path / "raw" / DAY.isoformat())
    assert len(loaded) == len(ordered)
    assert len(list((tmp_path / "raw" / DAY.isoformat()).glob("part-*.parquet"))) >= 2
    for a, b in zip(ordered, loaded):
        assert type(a) is type(b)
        if isinstance(a, Tick):
            assert (a.token, a.ts, a.recv_ts, a.ltp, a.volume, a.atp, a.oi, a.bid_px, a.ask_qty) == \
                   (b.token, b.ts, b.recv_ts, b.ltp, b.volume, b.atp, b.oi, b.bid_px, b.ask_qty)
    end = at(DAY, "10:35")
    direct = [event_line(e) for e in replay(processor([eq()]), ordered, end_ts=end)]
    stored = [event_line(e) for e in replay(processor([eq()]), loaded, end_ts=end)]
    assert direct == stored and any("SURGE_1" in x for x in direct)


def test_event_log_writer(tmp_path):
    w = EventLogWriter(tmp_path, DAY, config_version="abc")
    w.write([Event(kind="transition", ts=at(DAY, "10:22", 1.5), token=1, symbol="X", from_state="IDLE",
                   to_state="SURGE_1", reason="s1_confirmed", values={"x": 1.23456789})])
    w.close()
    rows = read_event_log(tmp_path / f"{DAY.isoformat()}.jsonl")
    assert rows[0]["to"] == "SURGE_1" and rows[0]["values"]["x"] == 1.234568
    assert rows[0]["config_version"] == "abc" and rows[0]["time"] == "10:22:01"


def test_sqlite_candle_store(tmp_path):
    proc = processor([eq()])
    candles = []
    proc.on_candles = candles.extend
    run(proc, surge1(script()), end="10:22:05")
    store = SQLiteCandleStore(tmp_path / "c.sqlite")
    store.write(candles, {EQ_TOKEN: "ABC"})
    store.write(candles, {EQ_TOKEN: "ABC"})                    # idempotent upsert
    back = store.read(EQ_TOKEN, at(DAY, "09:00"), at(DAY, "16:00"))
    assert len(back) == len(candles)
    assert back[-1].turnover_cr == pytest.approx(candles[-1].turnover_cr)
    bars = sessions_from_store(store, EQ_TOKEN, [DAY])[0]
    assert bars[-1].minute == 621 and bars[-1].turnover_cr == pytest.approx(5.0, rel=0.01)
    store.close()


@pytest.mark.postgres
@pytest.mark.skipif(not os.environ.get("IMBALANCE_TEST_PG_DSN"), reason="set IMBALANCE_TEST_PG_DSN to run")
def test_postgres_candle_store():
    from imbalance.storage.candles import PostgresCandleStore

    proc = processor([eq()])
    candles = []
    proc.on_candles = candles.extend
    run(proc, surge1(script()), end="10:22:05")
    store = PostgresCandleStore(os.environ["IMBALANCE_TEST_PG_DSN"])
    store.write(candles, {EQ_TOKEN: "ABC"})
    store.write(candles, {EQ_TOKEN: "ABC"})
    back = store.read(EQ_TOKEN, at(DAY, "09:00"), at(DAY, "16:00"))
    assert len(back) == len(candles) and back[-1].turnover_cr == pytest.approx(candles[-1].turnover_cr)
    store.close()


# --------------------------------------------------------------------------- runtime
async def test_runtime_with_simulated_feed(tmp_path):
    u = synthetic_universe(DAY, 12)
    m = SyntheticMarket(u, DAY, seed=3, pattern_every_minutes=1.0)
    cfgm = ConfigManager(data={"storage": {"data_dir": str(tmp_path)}})
    feed = SimulatedFeed(m.stream(at(DAY, "09:14", 50), at(DAY, "11:00")), speed=0)
    day = DayInfo.build(DAY, SessionConfig())
    bl = m.baselines()                       # fixed pre-market, as in live mode
    rt = Runtime(cfgm, u, bl, day, feed, live=False, data_dir=tmp_path, record_raw=True)
    q = rt.bus.subscribe()
    await rt.run()
    snap = rt.snapshot()
    assert set(snap) >= {"surge1", "pullback", "entries", "archive", "health", "alerts", "guardrails"}
    assert snap["health"]["feed"]["ticks"] > 50_000
    assert rt.dispatcher.counts["S1_CONFIRMED"] >= 1
    log_rows = read_event_log(tmp_path / "events" / f"{DAY.isoformat()}.jsonl")
    assert any(r["to"] == "SURGE_1" for r in log_rows)
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    assert {"alert", "events", "snapshot"} <= {x["topic"] for x in msgs}
    # the recorded day replays to the same event log
    recorded = read_raw(tmp_path / "raw" / DAY.isoformat())
    from imbalance.pipeline import Processor
    proc = Processor(cfgm, u, bl, day)
    again = [json.loads(event_line(e, cfgm.version)) for e in replay(proc, recorded, end_ts=rt.sim_time + 120,
                                                                     finish=True)]
    assert [r["seq"] for r in again] == [r["seq"] for r in log_rows]
    assert again == log_rows


async def test_runtime_feed_events_from_simulated_feed(tmp_path):
    s = gap_script()
    u = processor([eq()]).engine.universe
    from helpers import baselines
    cfgm = ConfigManager(data={})
    feed = SimulatedFeed(group_by_second(merge_records(s)), speed=0)
    rt = Runtime(cfgm, u, baselines([eq()]), DayInfo.build(DAY, SessionConfig()), feed, live=False,
                 data_dir=tmp_path, record_raw=False, event_log=False)
    await rt.run()
    reasons = [r["reason"] for r in rt.recent_events if r["kind"] == "system"]
    assert "feed_disconnect" in reasons and "feed_disconnected" in reasons and "feed_reconnect" in reasons
    assert [a["type"] for a in rt.dispatcher.recent if a["type"] == "SYSTEM"]
    assert not [a for a in rt.dispatcher.recent if a["type"] == "S1_CONFIRMED"]


# --------------------------------------------------------------------------- dispatcher
async def test_dispatcher_dedupe_mute_and_telegram():
    sent = []

    def handler(req: httpx.Request):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json={"ok": True})

    tg = TelegramSender("TOKEN", "42", "https://api.telegram.org", min_interval=0.0,
                        transport=httpx.MockTransport(handler))
    cfgm = ConfigManager(data={"alerts": {"telegram": {"enabled": True, "min_grade": "B"}}})
    bus = InMemoryBus()
    d = AlertDispatcher(lambda: cfgm.config, bus, tg, clock=lambda: at(DAY, "10:42", 1.7))

    s = pullback(surge1(script()))
    s.candle("10:28", 100.42, 100.72, 100.40, 100.70, 3.5, buy_share=0.7)
    events = run(processor([eq()], b_overrides={628: 0.875}), s, end="10:29:05")
    entry = by(events, alert="ENTRY")[0]
    entry.grade, entry.score = "A", 82.0
    d.submit([entry])
    d.submit([entry])                                   # duplicate
    assert d.counts["deduped"] == 1
    low = by(events, alert="S1_CONFIRMED")[0]
    low.grade = "C"
    d.submit([low])
    d.muted.add(EQ_TOKEN)
    arm = by(events, alert="ARMED")[0]
    arm.grade = "A"
    d.submit([arm])
    task = asyncio.create_task(tg.run())
    await asyncio.sleep(0.2)
    task.cancel()
    assert len(sent) == 1 and sent[0]["chat_id"] == "42"          # C grade filtered, muted symbol skipped
    text = sent[0]["text"]
    lines = text.splitlines()
    assert lines[0].startswith("ENTRY ▲ ABC 10:29:00 | Grade A (82)")
    assert lines[1].startswith("S1 10:21 ₹5.0 cr, RVOL 8.0x → pullback 40% on 20% of S1 volume")
    assert lines[2].startswith("S2 ₹3.5 cr, close 100.70 > trigger 100.45")
    assert lines[3].startswith("Entry 100.70 | Stop ")
    assert d.recent[-1]["sound"] is None and d.recent[0]["sound"] == "entry"


def test_alert_formats_cover_every_type():
    s = pullback(surge1(script()))
    s.candle("10:28", 100.42, 100.72, 100.40, 100.70, 3.5, buy_share=0.7)
    s.candle("10:29", 100.70, 101.40, 100.65, 101.35, 1.0, buy_share=0.6)
    s.candle("10:30", 101.35, 101.95, 101.30, 101.90, 1.0, buy_share=0.6)
    events = run(processor([eq()], b_overrides={628: 0.875}), s, end="10:31:05")
    texts = {e.alert: format_alert(e) for e in events if e.alert}
    assert {"S1_PROVISIONAL", "S1_CONFIRMED", "ARMED", "S2_PROVISIONAL", "ENTRY", "T1", "EXIT"} <= set(texts)
    assert all(isinstance(t, str) and t for t in texts.values())
