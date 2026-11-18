"""Command line: ``imbalance <command>``.

  live        daily live screener (Kite Connect), dashboard on :8000
  login       exchange a Kite request token for today's access token
  demo        synthetic market through the real engine + dashboard (no broker needed)
  replay      replay a recorded day (fast, or time-scaled with the dashboard)
  backtest    candle or recorded-tick backtest with metrics and comparisons
  universe    build and print today's universe from an instrument dump
  history     download 1-minute history from Kite for backtests
  paper-report paper-trading gate from the live event logs
  check-config validate the YAML configuration
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import ConfigManager
from .timeutil import IST, at, ist_date, parse_hhmm

log = logging.getLogger("imbalance")


class JsonFormatter(logging.Formatter):
    """One JSON object per log line (spec §8: structured logs)."""

    def format(self, record: logging.LogRecord) -> str:
        out = {"ts": round(record.created, 3), "level": record.levelname, "logger": record.name,
               "msg": record.getMessage()}
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out)


def _setup_logging(level: str, as_json: bool = False) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if as_json else
                         logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), handlers=[handler], force=True)


def _today() -> date:
    return datetime.now(IST).date()


async def _serve(app, host: str, port: int):
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off"))
    return asyncio.create_task(server.serve()), server


# --------------------------------------------------------------------------- demo
async def cmd_demo(args) -> int:
    from .adapters.synthetic import SimulatedFeed, SyntheticMarket, synthetic_universe
    from .api import AppState, create_app
    from .calendar import DayInfo
    from .runtime import Runtime

    demo_overrides = {} if args.keep_guardrails else {
        "params": {"max_daily_losses": 1000, "max_daily_loss_pct": 100.0, "max_open_positions": 50}}
    cfgm = ConfigManager(args.config, overlay=None, data=demo_overrides)
    day = _today()
    u = synthetic_universe(day, args.stocks, seed=args.seed)
    market = SyntheticMarket(u, day, seed=args.seed, rate=args.rate, pattern_every_minutes=args.pattern_every)
    start = at(day, args.start)
    feed = SimulatedFeed(market.stream(at(day, "09:14", 50), at(day, "15:41")), speed=args.speed,
                         fast_until=start)
    data_dir = Path(cfgm.config.storage.data_dir) / "demo"
    rt = Runtime(cfgm, u, market.baselines(), DayInfo.build(day, cfgm.config.session), feed, live=False,
                 data_dir=data_dir, record_raw=False, event_log=True)
    state = AppState(cfgm, runtime=rt)
    task, server = await _serve(create_app(state), args.host, args.port)
    print(f"Demo running on http://{args.host if args.host != '0.0.0.0' else 'localhost'}:{args.port} "
          f"({args.stocks} fictional stocks + futures, {args.speed}x speed from {args.start}). Ctrl-C to stop.")
    if not args.keep_guardrails:
        print("Daily guardrails are relaxed in the demo so entries keep flowing (--keep-guardrails to keep them).")
    await rt.run()
    print("Simulated session finished; dashboard stays up for review. Ctrl-C to exit.")
    await task
    return 0


# --------------------------------------------------------------------------- replay
async def cmd_replay(args) -> int:
    from .adapters.synthetic import SimulatedFeed, group_by_second
    from .calendar import DayInfo, EventCalendar, TradingCalendar
    from .daily import load_day_reference
    from .pipeline import Processor, replay
    from .storage.eventlog import event_line
    from .storage.raw import raw_dir, read_raw

    cfgm = ConfigManager(args.config)
    cfg = cfgm.config
    d = date.fromisoformat(args.date)
    data_dir = Path(args.data_dir or cfg.storage.data_dir)
    universe, baselines = load_day_reference(data_dir, d)
    records = read_raw(raw_dir(data_dir, d))
    if not records:
        print(f"no recorded data for {d}", file=sys.stderr)
        return 1
    cal = TradingCalendar.from_config(cfg.calendar)
    day = DayInfo.build(d, cfg.session, cal, EventCalendar.load(cfg.calendar.events_file))
    if not args.serve:
        t0 = time.perf_counter()
        proc = Processor(cfgm, universe, baselines, day)
        events = replay(proc, records, end_ts=records[-1].recv_ts + 120, finish=True)
        out = Path(args.out or f"replay-{d.isoformat()}.jsonl")
        with out.open("w", encoding="utf-8") as fh:
            for e in events:
                fh.write(event_line(e, cfgm.version) + "\n")
        n_ticks = sum(1 for r in records if hasattr(r, "ltp"))
        print(f"replayed {n_ticks} snapshots in {time.perf_counter() - t0:.1f}s -> {len(events)} events in {out}")
        return 0
    from .api import AppState, create_app
    from .runtime import Runtime

    has_clock = any(getattr(r, "kind", None) == "clock" for r in records)
    feed = SimulatedFeed(group_by_second(records), speed=args.speed, recorded_clock=has_clock)
    rt = Runtime(cfgm, universe, baselines, day, feed, live=False, data_dir=data_dir / "replays",
                 record_raw=False, event_log=True, event_log_name=f"replay-{d.isoformat()}.jsonl")
    state = AppState(cfgm, runtime=rt)
    task, _ = await _serve(create_app(state), args.host, args.port)
    print(f"Replaying {d} at {args.speed}x on http://localhost:{args.port}. Ctrl-C to stop.")
    await rt.run()
    await task
    return 0


# --------------------------------------------------------------------------- live
async def cmd_live(args) -> int:
    from .adapters.kite import KiteRest, KiteTicker
    from .alerts import TelegramSender
    from .api import AppState, create_app
    from .bus import InMemoryBus, RedisBus
    from .calendar import DayInfo, TradingCalendar
    from .daily import prepare_live_day, save_day_reference
    from .runtime import Runtime
    from .storage.candles import open_store

    cfgm = ConfigManager(args.config)
    cfg = cfgm.config
    kc = cfg.broker.kite
    rest = KiteRest(kc)
    if not rest.api_key:
        print(f"set {kc.api_key_env} and {kc.api_secret_env} in the environment", file=sys.stderr)
        return 2
    state = AppState(cfgm, kite_rest=rest)
    server_task, _ = await _serve(create_app(state), args.host or cfg.api.host, args.port or cfg.api.port)
    redis_url = os.environ.get(cfg.bus.redis_url_env)
    bus = RedisBus(redis_url, cfg.bus.channel_prefix) if (cfg.bus.backend == "redis" and redis_url) else InMemoryBus()
    store = open_store(cfg.storage.candle_backend, cfg.storage.sqlite_path,
                       os.environ.get(cfg.storage.postgres_dsn_env))
    telegram = TelegramSender.from_config(cfg.alerts.telegram)
    tg_task = asyncio.create_task(telegram.run()) if telegram else None
    calendar = TradingCalendar.from_config(cfg.calendar)
    while True:
        today = _today()
        now = time.time()
        startup = at(today, cfg.session.startup_time)
        shutdown = at(today, cfg.session.shutdown_time)
        if (not calendar.is_trading_day(today) and calendar.special_session(today) is None) or now >= shutdown:
            state.status, state.message = "closed", "market closed; waiting for the next session"
            if not args.daemon:
                print("market closed today; use --daemon to wait for the next session")
                break
            nxt = calendar.next_trading_day(today)
            await asyncio.sleep(max(60.0, at(nxt, cfg.session.startup_time) - time.time()))
            continue
        if now < startup and not args.now:
            state.status, state.message = "waiting", f"start-up routine at {cfg.session.startup_time} IST"
            await asyncio.sleep(min(600.0, startup - now))
            continue
        # daily login: access tokens expire every day
        while rest.load_session(today) is None:
            state.status = "login_required"
            state.message = "Kite login required: open /kite/login (or run `imbalance login`)"
            if telegram:
                telegram.enqueue(f"SYSTEM: Kite login required for {today}. Open the screener's /kite/login.")
            try:
                await asyncio.wait_for(state.login_event.wait(), timeout=30.0)
            except asyncio.TimeoutError:
                pass
            state.login_event.clear()
        state.status, state.message = "preparing", "building universe and baselines"
        try:
            universe, baselines, events, calendar = await prepare_live_day(cfg, today, rest, store)
        except Exception as exc:
            log.exception("start-up routine failed")
            state.status, state.message = "error", f"start-up routine failed: {exc}"
            await asyncio.sleep(60)
            continue
        save_day_reference(cfg.storage.data_dir, universe, baselines)
        day = DayInfo.build(today, cfg.session, calendar, events)
        ticker = KiteTicker(rest.api_key, rest.access_token or "", kc)
        await ticker.subscribe(universe.tokens())
        rt = Runtime(cfgm, universe, baselines, day, ticker, live=True, candle_store=store, bus=bus,
                     telegram=None)
        rt.dispatcher.telegram = telegram
        state.runtime, state.status, state.message = rt, "running", None
        log.info("live: %d instruments (%d scannable)", len(universe.instruments), len(universe.scannable()))
        await rt.run(until_done=False)
        state.status, state.message = "closed", "session finished"
        if not args.daemon:
            break
    if tg_task:
        tg_task.cancel()
    if not args.daemon:
        print("session finished; dashboard stays up for review. Ctrl-C to exit.")
        await server_task
    return 0


async def cmd_login(args) -> int:
    from .adapters.kite import KiteRest

    cfg = ConfigManager(args.config).config
    rest = KiteRest(cfg.broker.kite)
    if not args.request_token:
        print("1. Open this URL and log in:\n   " + rest.login_url())
        print("2. Copy request_token from the redirect URL and run:\n   imbalance login --request-token <token>")
        return 0
    token = args.request_token
    if "request_token=" in token:
        from urllib.parse import parse_qs, urlparse
        token = parse_qs(urlparse(token).query)["request_token"][0]
    data = await rest.generate_session(token)
    p = rest.save_session(data)
    await rest.aclose()
    print(f"saved today's session to {p}")
    return 0


async def cmd_universe(args) -> int:
    from .calendar import TradingCalendar
    from .instruments import build_universe, load_ban_list, parse_instrument_master

    cfg = ConfigManager(args.config).config
    if args.instruments:
        text = Path(args.instruments).read_text(encoding="utf-8")
    else:
        from .adapters.kite import KiteRest
        rest = KiteRest(cfg.broker.kite)
        if rest.load_session(_today()) is None:
            print("no Kite session today; pass --instruments <dump.csv> or run `imbalance login`", file=sys.stderr)
            return 2
        text = await rest.instruments_csv()
    d = date.fromisoformat(args.date) if args.date else _today()
    ban, src = load_ban_list(cfg.universe, fetch=not args.offline)
    u = build_universe(parse_instrument_master(text), d, TradingCalendar.from_config(cfg.calendar), ban,
                       cfg.base_params(), cfg.universe)
    kinds: dict[str, int] = {}
    for i in u.instruments.values():
        kinds[i.kind.value] = kinds.get(i.kind.value, 0) + 1
    print(f"{d}: {len(u.underlyings)} F&O stocks, instruments by kind {kinds}, banned ({src}): {sorted(u.banned)}")
    if args.out:
        Path(args.out).write_text(json.dumps(u.to_dict(), indent=1), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


async def cmd_history(args) -> int:
    from .adapters.kite import KiteRest
    from .backtest.data import save_history

    cfg = ConfigManager(args.config).config
    rest = KiteRest(cfg.broker.kite)
    if rest.load_session(_today()) is None:
        print("no Kite session today; run `imbalance login` first", file=sys.stderr)
        return 2
    universe = json.loads(Path(args.universe).read_text(encoding="utf-8"))
    end = datetime.now(IST).replace(tzinfo=None)
    start = end - timedelta(days=args.days)
    out = Path(args.out or Path(cfg.storage.data_dir) / "history")
    for raw in universe["instruments"]:
        rows = await rest.historical(raw["token"], start, end, "minute", oi=raw["kind"] != "EQ")
        save_history(out, raw, rows)
        print(f"{raw['symbol']}: {len(rows)} candles")
    await rest.aclose()
    return 0


async def cmd_backtest(args) -> int:
    from .backtest.cli import run_backtest_cli

    return await asyncio.to_thread(run_backtest_cli, args)


def cmd_paper_report(args) -> int:
    """Paper-trading gate (§11): live alerts with simulated fills over >= 4 weeks
    and >= 50 entry signals, expectancy positive after costs."""
    from .backtest.metrics import TradeRecord, summarize
    from .storage.eventlog import read_event_log

    cfg = ConfigManager(args.config).config
    d0 = date.fromisoformat(args.start) if args.start else None
    d1 = date.fromisoformat(args.end) if args.end else None
    trades, days = [], set()
    for path in sorted(Path(args.events or cfg.storage.event_log_dir).glob("*.jsonl")):
        try:
            d = date.fromisoformat(path.stem)
        except ValueError:
            continue
        if (d0 and d < d0) or (d1 and d > d1):
            continue
        days.add(d)
        for r in read_event_log(path):
            v = r.get("values") or {}
            if r["kind"] == "trade" and r["reason"] == "closed" and not v.get("shadow"):
                trades.append(TradeRecord(id=v.get("trade_id", ""), token=r["token"], display=r["display"],
                                          kind="FUT" if "FUT" in r["display"] else "EQ", direction=r["direction"],
                                          entry_ts=r["ts"], closed_ts=r["ts"], gross_r=v.get("gross_r") or 0.0,
                                          net_r=v.get("net_r") or 0.0, net_pnl=v.get("net_pnl") or 0.0,
                                          mae_r=v.get("mae_r") or 0.0, mfe_r=v.get("mfe_r") or 0.0,
                                          exit_reason=v.get("exit_reason") or "", grade=r.get("grade")))
    s = summarize(trades)
    weeks = (max(days) - min(days)).days / 7 if days else 0
    gate = bool(days) and weeks >= 4 and s.get("trades", 0) >= 50 and (s.get("expectancy_r") or 0) > 0
    print(json.dumps({"sessions": len(days), "weeks": round(weeks, 1), "summary": s,
                      "gate_passed": gate,
                      "gate": "needs >= 4 weeks, >= 50 entries and positive net expectancy; then compare with the "
                              "backtest range before using real money"}, indent=1, default=str))
    return 0


def cmd_check_config(args) -> int:
    mgr = ConfigManager(args.config)
    cfg = mgr.config
    print(f"config OK (version {mgr.version}); active preset: {cfg.active_preset or 'none'}; "
          f"{len(cfg.symbol_overrides)} symbol overrides")
    return 0


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="imbalance", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="YAML config (default config/default.yaml)")
    ap.add_argument("--log-level", default="info")
    ap.add_argument("--log-json", action="store_true", help="structured JSON log lines")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("live", help="run the live screener (Kite Connect)")
    p.add_argument("--host", default=None)
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--daemon", action="store_true", help="keep running across trading days")
    p.add_argument("--now", action="store_true", help="skip waiting for the start-up time")

    p = sub.add_parser("login", help="Kite login: print the URL or exchange a request token")
    p.add_argument("--request-token", default=None, help="request_token or the full redirect URL")

    p = sub.add_parser("demo", help="synthetic market + dashboard (no broker needed)")
    p.add_argument("--stocks", type=int, default=40)
    p.add_argument("--speed", type=float, default=10.0, help="simulated seconds per real second")
    p.add_argument("--start", default="09:45", help="fast-forward to this IST time first")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--rate", type=float, default=1.0, help="snapshots per instrument per second")
    p.add_argument("--pattern-every", type=float, default=3.0, help="minutes between injected setups")
    p.add_argument("--keep-guardrails", action="store_true", help="keep the configured daily guardrails")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("replay", help="replay a recorded day")
    p.add_argument("--date", required=True)
    p.add_argument("--data-dir", default=None)
    p.add_argument("--out", default=None, help="event log output (fast mode)")
    p.add_argument("--serve", action="store_true", help="replay through the dashboard")
    p.add_argument("--speed", type=float, default=10.0)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)

    p = sub.add_parser("backtest", help="backtest (candles or recorded ticks)")
    from .backtest.cli import add_backtest_args
    add_backtest_args(p)

    p = sub.add_parser("universe", help="build today's universe")
    p.add_argument("--instruments", default=None, help="instrument dump CSV (else fetched from Kite)")
    p.add_argument("--date", default=None)
    p.add_argument("--offline", action="store_true", help="do not fetch the ban list")
    p.add_argument("--out", default=None)

    p = sub.add_parser("history", help="download 1-minute history from Kite")
    p.add_argument("--universe", required=True, help="universe JSON (from `imbalance universe --out`)")
    p.add_argument("--days", type=int, default=365)
    p.add_argument("--out", default=None)

    p = sub.add_parser("paper-report", help="paper-trading gate from live event logs")
    p.add_argument("--events", default=None, help="event log directory (default storage.event_log_dir)")
    p.add_argument("--from", dest="start", default=None)
    p.add_argument("--to", dest="end", default=None)

    sub.add_parser("check-config", help="validate the configuration")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.log_level, args.log_json)
    if args.cmd == "check-config":
        return cmd_check_config(args)
    if args.cmd == "paper-report":
        return cmd_paper_report(args)
    fn = {"live": cmd_live, "login": cmd_login, "demo": cmd_demo, "replay": cmd_replay, "backtest": cmd_backtest,
          "universe": cmd_universe, "history": cmd_history}[args.cmd]
    try:
        return asyncio.run(fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
