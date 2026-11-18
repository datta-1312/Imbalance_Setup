"""FastAPI backend and backtest runner."""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from helpers import DAY
from imbalance.adapters.synthetic import SimulatedFeed, SyntheticMarket, synthetic_universe
from imbalance.api import AppState, create_app
from imbalance.backtest.data import load_history, save_history
from imbalance.backtest.report import report_markdown
from imbalance.backtest.runner import CandleBacktest, TickBacktest, with_params
from imbalance.backtest.synthetic import synthetic_history
from imbalance.backtest.walkforward import add_months, walk_forward
from imbalance.calendar import DayInfo
from imbalance.config import AppConfig, ConfigManager, SessionConfig
from imbalance.daily import save_day_reference
from imbalance.runtime import Runtime
from imbalance.timeutil import at


@pytest.fixture(scope="module")
def finished_runtime(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("rt")
    u = synthetic_universe(DAY, 10)
    m = SyntheticMarket(u, DAY, seed=4, pattern_every_minutes=1.0)
    bl = m.baselines()
    cfgm = ConfigManager(data={"storage": {"data_dir": str(tmp)}, "api": {"static_dir": str(tmp / "nodist")}},
                         overlay=tmp / "local.yaml")
    feed = SimulatedFeed(m.stream(at(DAY, "09:14", 50), at(DAY, "11:30")), speed=0)
    rt = Runtime(cfgm, u, bl, DayInfo.build(DAY, SessionConfig()), feed, live=False, data_dir=tmp, record_raw=True)
    asyncio.run(rt.run())
    save_day_reference(tmp, u, bl)
    return rt, cfgm, tmp


def test_api_read_endpoints(finished_runtime):
    rt, cfgm, _ = finished_runtime
    client = TestClient(create_app(AppState(cfgm, runtime=rt)))
    snap = client.get("/api/snapshot").json()
    assert {"surge1", "pullback", "entries", "archive", "health", "alerts"} <= set(snap)
    assert client.get("/api/health").json()["mode"] == "simulated"
    sid = next(iter(rt.engine.setups))
    d = client.get(f"/api/setups/{sid}").json()
    assert d["setup"]["id"] == sid and d["instrument"]["candles"] and any(e["setup_id"] == sid for e in d["events"])
    assert client.get("/api/setups/nope").status_code == 404
    insts = client.get("/api/instruments").json()
    assert len(insts) == len(rt.engine.trackers)
    tok = insts[0]["token"]
    assert client.get(f"/api/instruments/{tok}").json()["token"] == tok
    assert client.get("/api/alerts?limit=5").status_code == 200
    assert "<h3>" in client.get("/").text                       # dashboard not built in this test


def test_api_config_edit_and_controls(finished_runtime, monkeypatch):
    rt, cfgm, tmp = finished_runtime
    client = TestClient(create_app(AppState(cfgm, runtime=rt)))
    cfg = client.get("/api/config").json()
    assert cfg["params"]["surge_rvol_min"] == 5.0 and "surge_rvol_min" in cfg["meta"]
    assert client.put("/api/config", json={"params": {"surge_rvol_min": "high"}}).status_code == 400
    assert client.put("/api/config", json={"params": {"nope": 1}}).status_code == 400
    assert client.put("/api/config", json={"broker": {}}).status_code == 400
    r = client.put("/api/config", json={"params": {"surge_rvol_min": 6.0}})
    assert r.status_code == 200 and r.json()["params"]["surge_rvol_min"] == 6.0
    assert (tmp / "local.yaml").exists() and cfgm.config.params.surge_rvol_min == 6.0
    r = client.put("/api/config", json={"active_preset": "conservative"})
    assert r.status_code == 400                                   # no presets in this inline config
    tok = next(iter(rt.engine.trackers))
    assert client.post(f"/api/symbols/{tok}/mute", json={"value": True}).json()["muted"] is True
    assert tok in rt.dispatcher.muted
    assert client.post(f"/api/symbols/{tok}/pause", json={"value": True}).json()["paused"] is True
    assert rt.engine.trackers[tok].paused
    assert client.post("/api/symbols/1/pause", json={"value": True}).status_code == 404
    # optional bearer token protects write endpoints
    monkeypatch.setenv("IMBALANCE_API_TOKEN", "s3cret")
    assert client.post(f"/api/symbols/{tok}/mute", json={"value": False}).status_code == 401
    ok = client.post(f"/api/symbols/{tok}/mute", json={"value": False}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200 and tok not in rt.dispatcher.muted


def test_api_websocket_and_starting_state(finished_runtime):
    rt, cfgm, _ = finished_runtime
    client = TestClient(create_app(AppState(cfgm, runtime=rt)))
    with client.websocket_connect("/ws") as ws:
        first = json.loads(ws.receive_text())
        assert first["topic"] == "snapshot" and "entries" in first["data"]
    starting = TestClient(create_app(AppState(cfgm, runtime=None)))
    assert starting.get("/api/snapshot").status_code == 503
    assert starting.get("/api/status").json()["status"] == "starting"


# --------------------------------------------------------------------------- backtest
@pytest.fixture(scope="module")
def history():
    return synthetic_history(n_stocks=4, days=24, seed=3)


def test_candle_backtest_report_and_comparison(history):
    cfg = AppConfig.from_dict({})
    res = CandleBacktest(cfg, history).run()
    rep = res.report()
    f = rep["funnel"]
    assert f["surge1"] > 0 and f["armed"] > 0 and f["surge2"] > 0
    assert rep["summary"]["trades"] > 0
    comp = rep["comparison"]
    assert comp["enter_at_surge1"]["trades"] > 0 and comp["random_same_time_of_day"]["trades"] > 0
    assert set(rep["splits"]) >= {"grade", "time_of_day", "instrument", "oi_reading", "event_day", "regime"}
    md = report_markdown(rep)
    assert "The core test" in md and "Enter at Surge 1" in md
    # deterministic
    again = CandleBacktest(cfg, history).run()
    assert [e.to_record() for d in res.days for e in d.events] == [e.to_record() for d in again.days for e in d.events]


def test_no_look_ahead(history):
    cfg = AppConfig.from_dict({})
    days = sorted({d for h in history.values() for d in h.days})
    target = days[15]
    base = CandleBacktest(cfg, history, compare=False).run_day(target)
    # corrupt everything after the target day: results for the target day must not change
    import copy
    future = copy.deepcopy(history)
    for h in future.values():
        for d in days[16:]:
            for c in h.days[d]:
                c.turnover_cr *= 50
                c.close *= 1.1
    after = CandleBacktest(cfg, future, compare=False).run_day(target)
    assert [e.to_record() for e in base.events] == [e.to_record() for e in after.events]


def test_history_roundtrip(tmp_path, history):
    tok, h = next(iter(history.items()))
    rows = [{"ts": c.start, "open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume,
             "oi": c.oi_close} for d in sorted(h.days) for c in h.days[d]]
    raw = {"token": tok, "symbol": h.inst.symbol, "underlying": h.inst.underlying, "kind": h.inst.kind.value,
           "lot_size": h.inst.lot_size, "tick_size": h.inst.tick_size}
    save_history(tmp_path, raw, rows)
    loaded = load_history(tmp_path)[tok]
    d0 = sorted(h.days)[0]
    assert len(loaded.days[d0]) == len(h.days[d0])
    assert loaded.days[d0][10].turnover_cr == pytest.approx(h.days[d0][10].turnover_cr)


def test_walk_forward_small(history):
    cfg = AppConfig.from_dict({})
    days = sorted({d for h in history.values() for d in h.days})
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    wf = walk_forward(cfg, history, {"surge_rvol_min": [4.0, 5.0]}, train_months=1, test_months=1, min_trades=1,
                      start=days[0], end=days[-1])
    assert "sensitivity" in wf and set(wf["sensitivity"]["surge_rvol_min"]) == {"4.0", "5.0"}
    assert wf["sensitivity"]["surge_rvol_min"]["5.0"]["default"] is True
    assert with_params(cfg, surge_rvol_min=4.0).params.surge_rvol_min == 4.0


def test_tick_backtest_on_recorded_day(finished_runtime):
    rt, cfgm, tmp = finished_runtime
    res = TickBacktest(AppConfig.from_dict({}), tmp).run()
    assert [d.date for d in res.days] == [DAY]
    rep = res.report()
    assert rep["funnel"]["surge1"] > 0 and rep["mode"] == "ticks"


def test_paper_report_from_event_logs(finished_runtime, capsys):
    import argparse

    from imbalance.cli import cmd_paper_report

    rt, cfgm, tmp = finished_runtime
    cmd_paper_report(argparse.Namespace(config=None, events=str(tmp / "events"), start=None, end=None))
    out = json.loads(capsys.readouterr().out)
    assert out["sessions"] == 1 and out["gate_passed"] is False
    assert out["summary"].get("trades", 0) == sum(1 for t in rt.engine.trades.trades.values()
                                                  if t.status == "closed" and not t.shadow)
