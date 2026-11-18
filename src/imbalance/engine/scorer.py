"""Grade (§5.6): a 0–100 score from configurable component weights and penalties.

Each component is a value in [0, 1] or ``None`` when it does not apply (e.g. OI
for a cash share). The score is the weighted mean over the components that
apply, times 100, minus penalties, clamped to [0, 100].
"""

from __future__ import annotations

from typing import Iterable, Mapping

from ..config import GradingConfig, Params


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else 1.0 if x > 1 else x


def ramp(value: float | None, lo: float, hi: float) -> float | None:
    if value is None:
        return None
    if hi <= lo:
        return 1.0 if value >= hi else 0.0
    return clamp01((value - lo) / (hi - lo))


def grade_letter(score: float, params: Params) -> str:
    if score >= params.grade_a_min:
        return "A"
    if score >= params.grade_b_min:
        return "B"
    return "C"


def compute_score(components: Mapping[str, float | None], penalties: Iterable[str],
                  grading: GradingConfig, params: Params) -> dict:
    num = 0.0
    den = 0.0
    used = {}
    for name, val in components.items():
        w = grading.weights.get(name, 0.0)
        if val is None or w <= 0:
            continue
        v = clamp01(float(val))
        num += w * v
        den += w
        used[name] = round(v, 4)
    base = 100.0 * num / den if den > 0 else 0.0
    pens = {p: grading.penalties.get(p, 0.0) for p in sorted(set(penalties))}
    score = max(0.0, min(100.0, base - sum(pens.values())))
    score = round(score, 1)
    return {"score": score, "grade": grade_letter(score, params), "base": round(base, 2),
            "components": used, "penalties": pens}


def rvol_component(rvol: float | None, rule_min: float, grading: GradingConfig) -> float | None:
    return ramp(rvol, rule_min, rule_min * grading.rvol_full_multiple)


def buy_share_component(share: float | None, rule_min: float, grading: GradingConfig) -> float | None:
    return ramp(share, rule_min, grading.buy_share_full)


def book_component(ratio: float | None, grading: GradingConfig) -> float | None:
    return ramp(ratio, 1.0, grading.book_pressure_full)


def regime_component(change_pct: float | None, direction: int, params: Params) -> float | None:
    if change_pct is None:
        return None
    c = change_pct * direction
    if c >= 0:
        return 1.0
    if c >= -params.nifty_down_limit_pct:
        return 0.5
    return 0.0


OI_SURGE_SCORE = {"buildup": 1.0, "flat": 0.5, "covering": 0.0}
OI_PULLBACK_SCORE = {"mild_unwinding": 1.0, "flat": 1.0, "rising": 0.5, "heavy_unwinding": 0.0}
