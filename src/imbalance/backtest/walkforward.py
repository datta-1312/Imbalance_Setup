"""Walk-forward tuning (spec §11 Method): fit on N months, test on the next M,
roll forward; then report how sensitive results are to each parameter."""

from __future__ import annotations

import itertools
import logging
from datetime import date
from typing import Any, Callable

from ..config import AppConfig
from .data import InstrumentHistory
from .metrics import summarize
from .runner import CandleBacktest, run_days, with_params

log = logging.getLogger(__name__)


def add_months(d: date, n: int) -> date:
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    for day in (d.day, 28, 27, 26):
        try:
            return date(year, month, min(day, 31))
        except ValueError:
            continue
    return date(year, month, 1)


def _objective(trades, min_trades: int) -> float:
    s = summarize(trades)
    if s.get("trades", 0) < min_trades:
        return float("-inf")
    return s["expectancy_r"]


def walk_forward(cfg: AppConfig, history: dict[int, InstrumentHistory], grid: dict[str, list[Any]],
                 train_months: int = 6, test_months: int = 2, min_trades: int = 20,
                 start: date | None = None, end: date | None = None, calendar=None, events=None,
                 progress: Callable[[str], None] | None = None) -> dict:
    cache: dict = {}
    base = CandleBacktest(cfg, history, calendar, events, compare=False, baseline_cache=cache)
    days = base.trading_days(start, end)
    if not days:
        return {"windows": [], "note": "no data"}
    keys = sorted(grid)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*(grid[k] for k in keys))] or [{}]
    runners = {i: CandleBacktest(with_params(cfg, **c), history, calendar, events, compare=False,
                                 baseline_cache=cache) for i, c in enumerate(combos)}
    windows = []
    oos_trades = []
    w_start = days[0]
    while True:
        train_end = add_months(w_start, train_months)
        test_end = add_months(train_end, test_months)
        train = [d for d in days if w_start <= d < train_end]
        test = [d for d in days if train_end <= d < test_end]
        if not train or not test:
            break
        scores = []
        for i, combo in enumerate(combos):
            res = run_days(runners[i], train)
            alerted = [t for t in res.trades if not t.shadow]
            scores.append((_objective(alerted, min_trades), i))
            if progress:
                progress(f"window {w_start}..{test_end} combo {i + 1}/{len(combos)}")
        best_score, best_i = max(scores, key=lambda x: (x[0], -x[1]))
        test_res = run_days(runners[best_i], test)
        test_trades = [t for t in test_res.trades if not t.shadow]
        oos_trades += test_trades
        windows.append({
            "train": [train[0].isoformat(), train[-1].isoformat()],
            "test": [test[0].isoformat(), test[-1].isoformat()],
            "best_params": combos[best_i],
            "train_expectancy_r": None if best_score == float("-inf") else round(best_score, 4),
            "test": summarize(test_trades),
        })
        w_start = add_months(w_start, test_months)
    # sensitivity over the whole period, one parameter at a time around the defaults
    sensitivity: dict[str, dict[str, Any]] = {}
    base_params = cfg.base_params()
    for k in keys:
        sensitivity[k] = {}
        for v in grid[k]:
            bt = CandleBacktest(with_params(cfg, **{k: v}), history, calendar, events, compare=False,
                                baseline_cache=cache)
            res = run_days(bt, days)
            s = summarize([t for t in res.trades if not t.shadow])
            sensitivity[k][str(v)] = {"trades": s.get("trades", 0), "expectancy_r": s.get("expectancy_r"),
                                      "win_rate": s.get("win_rate"), "default": v == getattr(base_params, k)}
    return {"grid": grid, "windows": windows, "out_of_sample": summarize(oos_trades), "sensitivity": sensitivity}
