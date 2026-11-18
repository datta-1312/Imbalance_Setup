"""Backtest report as JSON and a readable Markdown summary."""

from __future__ import annotations

from typing import Any


def _fmt(x: Any, digits: int = 2) -> str:
    if x is None:
        return "–"
    if isinstance(x, float):
        return f"{x:.{digits}f}"
    return str(x)


def _row(name: str, s: dict) -> str:
    if not s or not s.get("trades"):
        return f"| {name} | 0 | – | – | – | – | – |"
    return (f"| {name} | {s['trades']} | {_fmt(s['win_rate'] * 100, 1)}% | {_fmt(s['expectancy_r'], 3)} | "
            f"{_fmt(s.get('profit_factor'))} | {_fmt(s['max_drawdown_r'])} | {_fmt(s['avg_mae_r'])} / {_fmt(s['avg_mfe_r'])} |")


HEAD = "| Group | Trades | Win rate | Expectancy (R, net) | Profit factor | Max DD (R) | MAE / MFE (R) |\n|---|---|---|---|---|---|---|"


def report_markdown(rep: dict, wf: dict | None = None) -> str:
    f = rep["funnel"]
    lines = [
        f"# Backtest report ({rep['mode']})",
        "",
        f"{rep['days']} sessions, {rep.get('first_day')} → {rep.get('last_day')}; config {rep['config_version']}. "
        "Fills at candle prices plus the configured slippage; costs from the configured table.",
        "",
        "## Funnel",
        "",
        f"Surge 1 **{f['surge1']}** → armed **{f['armed']}** → Surge 2 **{f['surge2']}** "
        f"(alerted {f['entries']}, suppressed by guardrails {f['suppressed']}, risk out of range {f['risk_out_of_range']}) "
        f"→ winners **{f.get('winners', 0)}**. Invalidated {f['invalidated']}, expired {f['expired']}.",
        "",
        "## The core test: does waiting for Surge 2 add value?",
        "",
        HEAD,
        _row("Enter at Surge 2 (the setup)", rep["comparison"]["enter_at_surge2"]),
        _row("Enter at Surge 1", rep["comparison"]["enter_at_surge1"]),
        _row("Random entries, same times of day", rep["comparison"]["random_same_time_of_day"]),
        "",
        "If entering at Surge 2 does not beat both comparisons after costs, the imbalance story is not adding value.",
        "",
    ]
    for name, groups in rep["splits"].items():
        lines += [f"## By {name.replace('_', ' ')}", "", HEAD]
        lines += [_row(k, v) for k, v in groups.items()]
        lines.append("")
    if wf:
        lines += ["## Walk-forward", "", "| Train | Test | Best parameters | Train expectancy | Test trades | Test expectancy |",
                  "|---|---|---|---|---|---|"]
        for w in wf.get("windows", []):
            t = w["test"]
            lines.append(f"| {w['train'][0]}..{w['train'][1]} | {w['test'][0]}..{w['test'][1]} | "
                         f"{', '.join(f'{k}={v}' for k, v in w['best_params'].items())} | {_fmt(w['train_expectancy_r'], 3)} | "
                         f"{t.get('trades', 0)} | {_fmt(t.get('expectancy_r'), 3)} |")
        oos = wf.get("out_of_sample", {})
        lines += ["", f"Out-of-sample: {oos.get('trades', 0)} trades, expectancy {_fmt(oos.get('expectancy_r'), 3)} R.", "",
                  "### Sensitivity (whole period, one parameter at a time)", ""]
        for k, vals in wf.get("sensitivity", {}).items():
            cells = ", ".join(f"{v}: {_fmt(s['expectancy_r'], 3)}R/{s['trades']}" + (" (default)" if s["default"] else "")
                              for v, s in vals.items())
            lines.append(f"- **{k}**: {cells}")
        lines.append("")
    lines.append("Paper-trading gate: at least 4 weeks and 50 entry signals live with simulated fills before real money.")
    return "\n".join(lines)
