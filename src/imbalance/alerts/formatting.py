"""Alert text for the dashboard toast and Telegram (spec §9 example format)."""

from __future__ import annotations

from ..engine import Event
from ..timeutil import fmt_hms, minute_start

ARROW = {1: "▲", -1: "▼"}


def _cr(x) -> str:
    return "?" if x is None else f"₹{x:.1f} cr"


def _px(x) -> str:
    return "?" if x is None else f"{x:.2f}"


def _pct(x, digits: int = 0) -> str:
    return "?" if x is None else f"{x * 100:.{digits}f}%"


def _x(x) -> str:
    if x is None:
        return "?"
    if x == "inf":
        return "∞"
    return f"{x:.1f}x"


def display_time(e: Event) -> str:
    """Confirmed alerts show the candle-close boundary (e.g. 10:42:00); provisional
    and trade alerts show the snapshot time."""
    if e.kind in ("transition", "cancel", "update") and not e.provisional:
        return fmt_hms(minute_start(e.ts))
    return fmt_hms(e.ts)


def _grade(e: Event) -> str:
    return f" | Grade {e.grade} ({e.score:.0f})" if e.grade and e.score is not None else ""


def _oi(oi: dict | None) -> str | None:
    if not oi or oi.get("change_pct") is None:
        return None
    return f"OI {oi['change_pct']:+.1f}% {oi.get('label', '')}".rstrip()


def format_alert(e: Event) -> str:
    v = e.values
    a = e.alert
    arrow = ARROW.get(e.direction, "")
    head = f"{e.display} {display_time(e)}"
    if a == "ENTRY":
        s2 = v.get("s2") or {}
        plan = v.get("plan") or {}
        cmp = ">" if e.direction > 0 else "<"
        parts2 = [f"S2 {_cr(s2.get('turnover_cr'))}, close {_px(s2.get('close'))} {cmp} trigger {_px(s2.get('trigger'))}"]
        oi = _oi(v.get("oi"))
        if oi:
            parts2.append(oi)
        cf = v.get("cash_fut") or {}
        if cf.get("s2") or cf.get("s1"):
            parts2.append("cash confirms" if e.display.endswith("FUT") else "futures confirm")
        qty = plan.get("qty")
        lines = [
            f"ENTRY {arrow} {head}{_grade(e)}",
            f"S1 {v.get('s1_time')} {_cr(v.get('s1_turnover_cr'))}, RVOL {_x(v.get('s1_rvol'))} → pullback "
            f"{_pct(v.get('retracement'))} on {_pct(v.get('pullback_turnover_ratio'))} of S1 volume",
            " | ".join(parts2),
            f"Entry {_px(plan.get('entry'))} | Stop {_px(plan.get('stop'))} | T1 {_px(plan.get('t1'))} | "
            f"T2 {_px(plan.get('t2'))} | Qty {qty:,}" if qty is not None else "",
        ]
        if plan.get("note"):
            lines.append(f"Note: {plan['note']}")
        if v.get("tags"):
            lines.append("Tags: " + ", ".join(v["tags"]))
        return "\n".join(x for x in lines if x)
    if a == "S1_CONFIRMED":
        m = v.get("metrics") or {}
        bits = [f"{_cr(m.get('turnover_cr'))}, RVOL {_x(m.get('rvol'))}, sudden {_x(m.get('suddenness'))}",
                f"move {m.get('move_pct', 0) * e.direction:+.2f}%" if m.get("move_pct") is not None else None,
                f"{'buy' if e.direction > 0 else 'sell'} {_pct(m.get('buy_share'))}"]
        oi = _oi(v.get("oi"))
        tail = [x for x in (oi, "cash–futures confirm" if v.get("cash_fut") else None) if x]
        text = f"SURGE 1 {arrow} {head}{_grade(e)}\n" + ", ".join(b for b in bits if b)
        if tail:
            text += " | " + " | ".join(tail)
        if v.get("tags"):
            text += "\nTags: " + ", ".join(v["tags"])
        return text
    if a == "S1_PROVISIONAL":
        return (f"S1? {arrow} {head} (provisional) | {_cr(v.get('turnover_cr'))} so far, RVOL {_x(v.get('rvol'))}, "
                f"sudden {_x(v.get('suddenness'))} | LTP {_px(v.get('ltp'))}")
    if a == "S2_PROVISIONAL":
        cmp = ">" if e.direction > 0 else "<"
        return (f"S2? {arrow} {head} (provisional){_grade(e)} | {_cr(v.get('turnover_cr'))}, RVOL {_x(v.get('rvol'))}"
                f" | LTP {_px(v.get('ltp'))} {cmp} trigger {_px(v.get('trigger'))}")
    if a in ("S1_CANCELLED", "S2_CANCELLED"):
        stage = "S1" if a == "S1_CANCELLED" else "S2"
        return f"{stage} provisional cancelled {head}: {', '.join(map(str, v.get('failed') or []))}"
    if a == "ARMED":
        return (f"ARMED {arrow} {head}{_grade(e)}\npullback {_pct(v.get('retracement'))} on "
                f"{_pct(v.get('pullback_turnover_ratio'))} of S1 volume, {v.get('pullback_candles')} candles | "
                f"trigger {_px(v.get('P'))}")
    if a == "T1":
        return f"T1 ✓ {head} | booked {_pct(v.get('fraction'))} at {_px(v.get('price'))} | stop → entry"
    if a == "EXIT":
        r = v.get("r")
        return f"EXIT {head} | {e.reason} at {_px(v.get('price'))}" + (f" | {r:+.2f}R" if r is not None else "")
    if a == "EXIT_WARNING":
        tag = " (provisional)" if e.provisional else ""
        return (f"EXIT WARNING {ARROW.get(-e.direction, '')} {head}{tag} | sell-side surge "
                f"{_cr(v.get('turnover_cr'))}, {'sell' if e.direction > 0 else 'buy'} {_pct(v.get('sell_share'))}")
    if a in ("INVALIDATED", "EXPIRED"):
        return f"{a} {head}: {e.reason}"
    if a == "SYSTEM":
        return f"SYSTEM {fmt_hms(e.ts)}: {e.reason}" + (f" — {v['message']}" if v.get("message") else "")
    return f"{a} {head}"


SOUNDS = {"S1_PROVISIONAL": "provisional", "S2_PROVISIONAL": "provisional_s2", "S1_CONFIRMED": "confirmed",
          "ARMED": "armed", "ENTRY": "entry", "T1": "target", "EXIT": "exit", "EXIT_WARNING": "warning",
          "SYSTEM": "system"}
