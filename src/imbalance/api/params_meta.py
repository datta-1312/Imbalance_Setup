"""Parameter editor metadata: spec §10 ranges to test, grouped by section."""

from __future__ import annotations

PARAM_META: dict[str, dict] = {
    # Surge 1
    "surge_turnover_floor_cr": {"group": "Surge 1", "range": [3, 6], "unit": "₹ cr", "section": "5.1"},
    "surge_rvol_min": {"group": "Surge 1", "range": [3, 10], "unit": "x", "section": "5.1"},
    "suddenness_multiple": {"group": "Surge 1", "range": [3, 6], "unit": "x", "section": "5.1"},
    "surge_window_candles": {"group": "Surge 1", "range": [1, 2], "section": "5.1"},
    "surge_min_move_pct": {"group": "Surge 1", "range": [0.2, 0.8], "unit": "%", "section": "5.1"},
    "close_in_top_pct": {"group": "Surge 1", "range": [20, 40], "unit": "%", "section": "5.1, 5.3"},
    "buy_share_min": {"group": "Surge 1", "range": [0.55, 0.70], "section": "5.1, 5.3"},
    "baseline_sessions": {"group": "Baseline", "range": [10, 30], "section": "3"},
    # Pullback
    "pullback_min_retrace": {"group": "Pullback", "range": [0.20, 0.40], "section": "5.2"},
    "pullback_max_retrace": {"group": "Pullback", "range": [0.50, 0.786], "section": "5.4"},
    "pullback_floor": {"group": "Pullback", "options": ["s1_low", "s1_midpoint"], "section": "5.2, 5.4"},
    "pullback_max_turnover_ratio": {"group": "Pullback", "range": [0.25, 0.50], "section": "5.2"},
    "pullback_min_candles": {"group": "Pullback", "range": [2, 5], "section": "5.2"},
    "distribution_turnover_ratio": {"group": "Pullback", "range": [0.50, 0.80], "section": "5.4"},
    "trigger_lookback_candles": {"group": "Pullback", "range": [2, 5], "section": "5.2"},
    # Surge 2
    "s2_turnover_vs_s1": {"group": "Surge 2", "range": [0.40, 1.00], "section": "5.3"},
    "s2_floor_cr": {"group": "Surge 2", "range": [2, 5], "unit": "₹ cr", "section": "5.3"},
    "s2_rvol_min": {"group": "Surge 2", "range": [2, 6], "unit": "x", "section": "5.3"},
    "entry_mode": {"group": "Surge 2", "options": ["close", "buy_stop"], "section": "5.3"},
    "setup_expiry_minutes": {"group": "Expiry", "range": [20, 90], "unit": "min", "section": "5.5"},
    "cooldown_minutes": {"group": "Expiry", "range": [5, 30], "unit": "min", "section": "5.5"},
    # OI
    "oi_change_min_pct": {"group": "Futures OI", "range": [0.2, 1.0], "unit": "%", "section": "6"},
    # Session
    "no_signal_before": {"group": "Session", "range": ["09:15", "09:30"], "section": "4"},
    "last_surge1_time": {"group": "Session", "range": ["14:30", "15:05"], "section": "4"},
    "last_entry_time": {"group": "Session", "range": ["14:45", "15:10"], "section": "4"},
    "adtv_floor_cr": {"group": "Session", "range": [10, 100], "unit": "₹ cr", "section": "4"},
    # Risk
    "stop_buffer_atr": {"group": "Risk", "range": [0.10, 0.50], "unit": "ATR", "section": "7"},
    "min_risk_pct": {"group": "Risk", "range": [0.1, 0.3], "unit": "%", "section": "7"},
    "max_risk_pct": {"group": "Risk", "range": [0.8, 2.0], "unit": "%", "section": "7"},
    "risk_per_trade_pct": {"group": "Risk", "range": [0.25, 1.0], "unit": "%", "section": "7"},
    "capital": {"group": "Risk", "unit": "₹", "section": "7"},
    "t1_r": {"group": "Risk", "range": [1, 2], "unit": "R", "section": "7"},
    "t2_r": {"group": "Risk", "range": [2, 4], "unit": "R", "section": "7"},
    "time_stop_candles": {"group": "Risk", "range": [5, 20], "section": "7"},
    "max_daily_loss_pct": {"group": "Risk", "range": [1, 3], "unit": "%", "section": "7"},
    "max_open_positions": {"group": "Risk", "range": [1, 5], "section": "7"},
    "grade_a_min": {"group": "Grade", "section": "5.6"},
    "grade_b_min": {"group": "Grade", "section": "5.6"},
    "short_setup_enabled": {"group": "Direction", "options": [False, True], "section": "5.7"},
    "event_day_mode": {"group": "Events", "options": ["tag", "suppress", "require_a"], "section": "4"},
}
