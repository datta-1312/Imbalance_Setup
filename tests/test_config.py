from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from imbalance.config import AppConfig, ConfigManager, Params

ROOT = Path(__file__).resolve().parents[1]


def test_default_yaml_matches_spec_defaults():
    mgr = ConfigManager(ROOT / "config" / "default.yaml", overlay=None)
    p = mgr.config.params
    # Spot-check every §10 default.
    assert p.surge_turnover_floor_cr == 4.0
    assert p.surge_rvol_min == 5
    assert p.suddenness_multiple == 4
    assert p.surge_window_candles == 1
    assert p.surge_min_move_pct == 0.3
    assert p.close_in_top_pct == 30
    assert p.buy_share_min == 0.60
    assert p.baseline_sessions == 20
    assert p.pullback_min_retrace == 0.25
    assert p.pullback_max_retrace == 0.618
    assert p.pullback_floor == "s1_low"
    assert p.pullback_max_turnover_ratio == 0.35
    assert p.pullback_min_candles == 3
    assert p.distribution_turnover_ratio == 0.60
    assert p.trigger_lookback_candles == 3
    assert p.s2_turnover_vs_s1 == 0.60
    assert p.s2_floor_cr == 3.0
    assert p.s2_rvol_min == 3
    assert p.setup_expiry_minutes == 45
    assert p.cooldown_minutes == 15
    assert p.oi_change_min_pct == 0.5
    assert p.no_signal_before == "09:20"
    assert p.last_surge1_time == "15:00"
    assert p.last_entry_time == "15:05"
    assert p.adtv_floor_cr == 25
    assert p.stop_buffer_atr == 0.25
    assert (p.min_risk_pct, p.max_risk_pct) == (0.15, 1.2)
    assert p.risk_per_trade_pct == 0.5
    assert (p.t1_r, p.t2_r) == (1.5, 3)
    assert p.time_stop_candles == 10
    assert p.max_daily_loss_pct == 1.5
    assert p.max_open_positions == 3
    assert (p.grade_a_min, p.grade_b_min) == (75, 60)
    assert p.short_setup_enabled is False
    # The YAML and the code defaults agree exactly.
    assert p == Params()
    assert mgr.config.session.futures_end == "15:40"
    assert mgr.config.session.cash_closing_auction == ("15:15", "15:35")


def test_presets_and_symbol_overrides():
    cfg = AppConfig.from_dict({
        "presets": {"strict": {"surge_rvol_min": 7.0}},
        "active_preset": "strict",
        "symbol_overrides": {"RELIANCE": {"surge_turnover_floor_cr": 8.0},
                             "RELIANCE:FUT": {"s2_floor_cr": 5.0}},
    })
    assert cfg.params_for("INFY", "EQ").surge_rvol_min == 7.0
    eq = cfg.params_for("RELIANCE", "EQ")
    fut = cfg.params_for("RELIANCE", "FUT")
    assert eq.surge_turnover_floor_cr == 8.0 and eq.s2_floor_cr == 3.0
    assert fut.surge_turnover_floor_cr == 8.0 and fut.s2_floor_cr == 5.0
    assert fut.surge_rvol_min == 7.0
    assert cfg.params_for("RELIANCE", "EQ") is eq  # cached


def test_invalid_overrides_rejected():
    with pytest.raises(ValidationError):
        AppConfig.from_dict({"symbol_overrides": {"X": {"no_such_param": 1}}})
    with pytest.raises(ValidationError):
        AppConfig.from_dict({"active_preset": "missing"})
    with pytest.raises(ValidationError):
        AppConfig.from_dict({"params": {"no_signal_before": "25:00"}})
    with pytest.raises(ValidationError):
        AppConfig.from_dict({"params": {"surge_window_candles": 3}})


def test_hot_reload_and_overlay(tmp_path):
    base = tmp_path / "default.yaml"
    base.write_text(yaml.safe_dump({"params": {"surge_rvol_min": 5.0}}))
    mgr = ConfigManager(base)
    seen = []
    mgr.on_change(lambda c: seen.append(c.params.surge_rvol_min))
    v0 = mgr.version
    assert mgr.params_for("A", "EQ").surge_rvol_min == 5.0

    import os
    import time
    base.write_text(yaml.safe_dump({"params": {"surge_rvol_min": 6.0}}))
    os.utime(base, (time.time() + 5, time.time() + 5))
    assert mgr.reload_if_changed()
    assert mgr.params_for("A", "EQ").surge_rvol_min == 6.0
    assert mgr.version != v0

    # The dashboard writes to the overlay; invalid edits are refused without writing.
    mgr.write_overlay({"params": {"buy_share_min": 0.65}})
    assert mgr.config.params.buy_share_min == 0.65
    assert (tmp_path / "local.yaml").exists()
    with pytest.raises(ValidationError):
        mgr.write_overlay({"params": {"buy_share_min": "lots"}})
    assert mgr.config.params.buy_share_min == 0.65
    assert seen == [6.0, 6.0]

    # A broken file on disk is rejected and the previous config is kept.
    base.write_text("params: [not, a, mapping")
    os.utime(base, (time.time() + 10, time.time() + 10))
    assert not mgr.reload_if_changed()
    assert mgr.params_for("A", "EQ").surge_rvol_min == 6.0
