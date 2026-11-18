"""Pure rule evaluators (spec §5, §6). Inputs are candles in the setup's view
space (prices negated for the mirrored short setup), outputs are rule checklists
that are logged and shown in the dashboard."""

from __future__ import annotations

import math
from typing import Sequence

from ..config import Params
from ..models import UNTRADEABLE, Candle
from .types import Check, Evaluation, evaluation

INF = math.inf


def merge_window(cs: Sequence[Candle]) -> Candle:
    """Judge consecutive candles together (``surge_window_candles`` = 2)."""
    if len(cs) == 1:
        return cs[0]
    first, last = cs[0], cs[-1]
    m = Candle(token=first.token, start=first.start, open=first.open, close=last.close,
               high=max(c.high for c in cs), low=min(c.low for c in cs),
               turnover_cr=sum(c.turnover_cr for c in cs), volume=sum(c.volume for c in cs),
               buy_vol=sum(c.buy_vol for c in cs), sell_vol=sum(c.sell_vol for c in cs),
               unk_vol=sum(c.unk_vol for c in cs), vwap=last.vwap, oi_open=first.oi_open,
               oi_close=last.oi_close, n_ticks=sum(c.n_ticks for c in cs), total_buy_qty=last.total_buy_qty,
               total_sell_qty=last.total_sell_qty, bid5_qty=last.bid5_qty, ask5_qty=last.ask5_qty,
               prev_day_close=last.prev_day_close, closed=True)
    for c in cs:
        m.flags |= c.flags
    return m


def mean_prev_turnover(prev: Sequence[Candle], n: int) -> tuple[float, int]:
    """Mean turnover of the last ``n`` usable candles today (gap/no-data excluded)."""
    vals: list[float] = []
    for c in reversed(prev):
        if c.flags & UNTRADEABLE:
            continue
        vals.append(c.turnover_cr)
        if len(vals) >= n:
            break
    if not vals:
        return 0.0, 0
    return sum(vals) / len(vals), len(vals)


def suddenness(turnover: float, k: int, prev: Sequence[Candle], params: Params) -> float | None:
    mean, n = mean_prev_turnover(prev, params.suddenness_lookback_candles)
    if n < params.suddenness_min_candles:
        return None
    if mean <= 0:
        return INF if turnover > 0 else 0.0
    return turnover / (k * mean)


def pct_change(new: float, old: float | None) -> float | None:
    if not old:
        return None
    return (new - old) / abs(old) * 100.0


def surge1_checks(win: Candle, prev: Sequence[Candle], baseline_sum: float | None, k: int,
                  params: Params) -> Evaluation:
    """Rules 1–6 of §5.1 on a (possibly merged) window. Rule 7 (session, band,
    data quality) is supplied by the caller as eligibility checks."""
    T = win.turnover_cr
    rvol = (T / baseline_sum) if baseline_sum else None
    sud = suddenness(T, k, prev, params)
    ref = prev[-1].close if prev else win.prev_day_close
    move = pct_change(win.close, ref)
    top = 1.0 - params.close_in_top_pct / 100.0
    loc = win.close_location
    checks = [
        Check("turnover_floor_cr", T, params.surge_turnover_floor_cr, T >= params.surge_turnover_floor_cr),
        Check("rvol", rvol, params.surge_rvol_min, rvol is not None and rvol >= params.surge_rvol_min),
        Check("suddenness", sud, params.suddenness_multiple, sud is not None and sud >= params.suddenness_multiple),
        Check("green_close_in_top", loc, top, win.green and loc >= top),
        Check("move_pct", move, params.surge_min_move_pct, move is not None and move >= params.surge_min_move_pct),
        Check("buy_share", win.buy_share, params.buy_share_min, win.buy_share >= params.buy_share_min),
    ]
    metrics = {"turnover_cr": T, "rvol": rvol, "suddenness": sud, "close_location": loc, "move_pct": move,
               "buy_share": win.buy_share, "baseline_cr": baseline_sum, "window": k}
    return evaluation(checks, metrics)


def provisional_s1_checks(c: Candle, prev: Sequence[Candle], b_m: float | None, params: Params) -> Evaluation:
    """Running turnover of the forming candle crosses the surge thresholds and
    price is above the candle's open (§3)."""
    T = c.running_turnover_cr
    rvol = (T / b_m) if b_m else None
    sud = suddenness(T, 1, prev, params)
    checks = [
        Check("turnover_floor_cr", T, params.surge_turnover_floor_cr, T >= params.surge_turnover_floor_cr),
        Check("rvol", rvol, params.surge_rvol_min, rvol is not None and rvol >= params.surge_rvol_min),
        Check("suddenness", sud, params.suddenness_multiple, sud is not None and sud >= params.suddenness_multiple),
        Check("above_open", c.close, c.open, c.close > c.open),
    ]
    return evaluation(checks, {"turnover_cr": T, "rvol": rvol, "suddenness": sud, "ltp": c.close})


def surge2_checks(c: Candle, s1_turnover: float, b_m: float | None, trigger: float | None,
                  params: Params) -> Evaluation:
    """Rules 1–6 of §5.3 (rule 7, the entry-time window, comes from the caller)."""
    T = c.turnover_cr
    rvol = (T / b_m) if b_m else None
    ratio = T / s1_turnover if s1_turnover > 0 else None
    top = 1.0 - params.close_in_top_pct / 100.0
    loc = c.close_location
    checks = [
        Check("turnover_vs_s1", ratio, params.s2_turnover_vs_s1, ratio is not None and ratio >= params.s2_turnover_vs_s1),
        Check("turnover_floor_cr", T, params.s2_floor_cr, T >= params.s2_floor_cr),
        Check("rvol", rvol, params.s2_rvol_min, rvol is not None and rvol >= params.s2_rvol_min),
        Check("green_close_in_top", loc, top, c.green and loc >= top),
        Check("close_above_trigger", c.close, trigger, trigger is not None and c.close > trigger),
        Check("buy_share", c.buy_share, params.buy_share_min, c.buy_share >= params.buy_share_min),
    ]
    metrics = {"turnover_cr": T, "rvol": rvol, "turnover_vs_s1": ratio, "close_location": loc,
               "buy_share": c.buy_share, "close": c.close, "trigger": trigger, "baseline_cr": b_m}
    return evaluation(checks, metrics)


def provisional_s2_checks(c: Candle, s1_turnover: float, b_m: float | None, trigger: float | None,
                          params: Params) -> Evaluation:
    T = c.running_turnover_cr
    rvol = (T / b_m) if b_m else None
    checks = [
        Check("turnover_vs_s1", T / s1_turnover if s1_turnover else None, params.s2_turnover_vs_s1,
              s1_turnover > 0 and T >= params.s2_turnover_vs_s1 * s1_turnover),
        Check("turnover_floor_cr", T, params.s2_floor_cr, T >= params.s2_floor_cr),
        Check("rvol", rvol, params.s2_rvol_min, rvol is not None and rvol >= params.s2_rvol_min),
        Check("ltp_above_trigger", c.close, trigger, trigger is not None and c.close > trigger),
    ]
    return evaluation(checks, {"turnover_cr": T, "rvol": rvol, "ltp": c.close, "trigger": trigger})


def is_distribution(c: Candle, s1_turnover: float, params: Params) -> tuple[bool, dict]:
    """A red pullback candle with turnover >= 60% of S1 and sell share >= 60%."""
    ratio = c.turnover_cr / s1_turnover if s1_turnover > 0 else 0.0
    hit = (c.red and not (c.flags & UNTRADEABLE) and ratio >= params.distribution_turnover_ratio
           and c.sell_share >= params.distribution_sell_share_min)
    return hit, {"turnover_cr": c.turnover_cr, "turnover_vs_s1": ratio, "sell_share": c.sell_share}


def is_sell_surge(c: Candle, s1_turnover: float, params: Params) -> bool:
    """Exit warning after entry (§7): red, turnover >= 60% of S1, sell share >= 60%."""
    return (c.red and not (c.flags & UNTRADEABLE) and s1_turnover > 0
            and c.turnover_cr >= params.exit_warning_turnover_ratio * s1_turnover
            and c.sell_share >= params.exit_warning_sell_share_min)


# --------------------------------------------------------------------------- OI (§6)
def oi_change_pct(window: Sequence[Candle]) -> float | None:
    """ΔOI over a window of candles: OI at the window's start vs its end."""
    if not window:
        return None
    start = next((c.oi_open for c in window if c.oi_open), None)
    end = next((c.oi_close for c in reversed(window) if c.oi_close), None)
    if not start or end is None:
        return None
    return (end - start) / start * 100.0


def oi_reading_surge(dpct: float | None, params: Params) -> str | None:
    """Price up (a surge candle in view space) with OI up = build-up; with OI
    down = covering; otherwise flat."""
    if dpct is None:
        return None
    if dpct >= params.oi_change_min_pct:
        return "buildup"
    if dpct <= -params.oi_change_min_pct:
        return "covering"
    return "flat"


def oi_reading_pullback(dpct: float | None, params: Params) -> str | None:
    if dpct is None:
        return None
    if dpct <= -params.oi_heavy_unwind_pct:
        return "heavy_unwinding"
    if dpct <= -params.oi_change_min_pct:
        return "mild_unwinding"
    if dpct >= params.oi_change_min_pct:
        return "rising"
    return "flat"


OI_LABELS = {
    1: {"buildup": "long build-up", "covering": "short covering", "flat": "OI flat",
        "heavy_unwinding": "heavy long unwinding", "mild_unwinding": "long unwinding (profit booking)",
        "rising": "OI rising (short build-up risk)"},
    -1: {"buildup": "short build-up", "covering": "long unwinding", "flat": "OI flat",
         "heavy_unwinding": "heavy short covering", "mild_unwinding": "short covering (profit booking)",
         "rising": "OI rising (long build-up risk)"},
}


def oi_label(reading: str | None, direction: int) -> str:
    if reading is None:
        return "n/a"
    return OI_LABELS[1 if direction > 0 else -1].get(reading, reading)
