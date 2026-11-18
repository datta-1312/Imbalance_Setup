"""``imbalance backtest`` command."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path

from ..calendar import EventCalendar, TradingCalendar
from ..config import ConfigManager
from .report import report_markdown

log = logging.getLogger(__name__)


def add_backtest_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--mode", choices=["candles", "ticks"], default="candles")
    p.add_argument("--history", default=None, help="directory of <token>.parquet/.csv + <token>.json (candle mode)")
    p.add_argument("--data-dir", default=None, help="recorded data root (tick mode; default storage.data_dir)")
    p.add_argument("--synthetic-days", type=int, default=0,
                   help="ignore --history and run on synthetic history of this many days (pipeline check only)")
    p.add_argument("--from", dest="start", default=None)
    p.add_argument("--to", dest="end", default=None)
    p.add_argument("--no-compare", action="store_true", help="skip the Surge-1 and random-entry comparisons")
    p.add_argument("--walk-forward", action="store_true", help="walk-forward tuning over backtest.walk_forward.grid")
    p.add_argument("--out", default="reports", help="output directory for report.json / report.md / events.jsonl")


def run_backtest_cli(args) -> int:
    cfgm = ConfigManager(args.config)
    cfg = cfgm.config
    cal = TradingCalendar.from_config(cfg.calendar)
    ev = EventCalendar.load(cfg.calendar.events_file)
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    wf = None
    if args.mode == "ticks":
        from .runner import TickBacktest

        bt = TickBacktest(cfg, args.data_dir or cfg.storage.data_dir, cal, ev, compare=not args.no_compare)
        result = bt.run(start, end)
    else:
        from .data import load_history
        from .runner import CandleBacktest

        if args.synthetic_days:
            from .synthetic import synthetic_history
            history = synthetic_history(days=args.synthetic_days)
            print("NOTE: synthetic history — this checks the pipeline, not the trading idea.")
        elif args.history:
            history = load_history(args.history, cfg.broker.kite.futures_volume_unit == "lots")
        else:
            print("candle mode needs --history <dir> (see `imbalance history`) or --synthetic-days N")
            return 2
        bt = CandleBacktest(cfg, history, cal, ev, compare=not args.no_compare,
                            progress=lambda d: log.info("backtest %s", d))
        result = bt.run(start, end)
        if args.walk_forward:
            from .walkforward import walk_forward

            w = cfg.backtest.walk_forward
            wf = walk_forward(cfg, history, w.grid, w.train_months, w.test_months, w.min_trades, start, end, cal, ev,
                              progress=lambda s: log.info(s))
    rep = result.report()
    if wf is not None:
        rep["walk_forward"] = wf
    (out / "report.json").write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    (out / "report.md").write_text(report_markdown(rep, wf), encoding="utf-8")
    with (out / "events.jsonl").open("w", encoding="utf-8") as fh:
        for d in result.days:
            for e in d.events:
                fh.write(json.dumps(e.to_record(), sort_keys=True) + "\n")
    s = rep["summary"]
    print(f"{rep['days']} sessions; funnel {rep['funnel']}; alerted trades {s.get('trades', 0)}, "
          f"expectancy {s.get('expectancy_r')} R (net). Report: {out / 'report.md'}")
    return 0
