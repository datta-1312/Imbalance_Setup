"""Trade management after ENTRY (§7): T1 partial booking, stop to entry, trailing
below the last completed 3-minute candle low or VWAP, T2, time stop, sell-surge
exit warning, end-of-day exit, P&L with slippage and costs, daily guardrails.

Prices are kept in the setup's view space (negated for shorts), so one code
path handles both directions. In ``ticks`` mode stops and targets are checked
on every snapshot; in ``ohlc`` mode (candle-only backtests) each candle is walked
as O→L→H→C (green) or O→H→L→C (red).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

from ..config import AppConfig, Params
from ..costs import fill_costs, slipped
from ..models import Candle, Kind, view
from ..timeutil import minute_of_day
from .risk import RiskBook, TradePlan, round_down
from .rules import is_sell_surge
from .types import AlertType, Event

if TYPE_CHECKING:
    from ..calendar import SessionRules
    from ..models import Instrument


@dataclass
class Trade:
    id: str
    setup_id: str
    token: int
    symbol: str
    display: str
    kind: str
    is_future: bool
    direction: int
    tick: float
    lot_size: int
    entry_ts: float
    entry: float
    stop: float
    initial_stop: float
    R: float
    qty: int
    t1: float
    t2: float
    s2_high: float
    s1_turnover: float
    entry_candle_start: float
    planned_entry: float
    status: str = "open"          # pending | open | t1 | closed | cancelled
    fraction_open: float = 1.0
    exits: list[dict] = field(default_factory=list)
    candles_since_entry: int = 0
    pending_candles: int = 0
    new_high: bool = False
    trail_low: float | None = None
    mfe: float = 0.0
    mae: float = 0.0
    last: float = 0.0
    shadow: bool = False
    suppressed: str | None = None
    grade: str | None = None
    score: float | None = None
    exit_reason: str | None = None
    closed_ts: float | None = None
    gross_r: float | None = None
    net_r: float | None = None
    gross_pnl: float | None = None
    net_pnl: float | None = None
    costs: float | None = None
    stat_qty: int = 0
    note: str | None = None
    cash_fut: bool = False
    tags: list[str] = field(default_factory=list)
    warned_minute: float | None = None

    def real(self, x: float | None) -> float | None:
        return None if x is None else round(self.direction * x, 6)

    @property
    def is_active(self) -> bool:
        return self.status in ("pending", "open", "t1")

    def row(self) -> dict:
        return {
            "id": self.id, "setup_id": self.setup_id, "token": self.token, "symbol": self.symbol,
            "display": self.display, "kind": self.kind, "direction": self.direction, "status": self.status,
            "entry_ts": self.entry_ts, "entry": self.real(self.entry), "stop": self.real(self.stop),
            "initial_stop": self.real(self.initial_stop), "t1": self.real(self.t1), "t2": self.real(self.t2),
            "qty": self.qty, "R": round(self.R, 6), "fraction_open": round(self.fraction_open, 4),
            "last": self.real(self.last), "open_r": round((self.last - self.entry) / self.R, 3) if self.R else None,
            "mfe_r": round(self.mfe / self.R, 3) if self.R else None,
            "mae_r": round(self.mae / self.R, 3) if self.R else None,
            "exits": [dict(e, price=self.real(e["price"])) for e in self.exits],
            "exit_reason": self.exit_reason, "closed_ts": self.closed_ts, "gross_r": self.gross_r,
            "net_r": self.net_r, "gross_pnl": self.gross_pnl, "net_pnl": self.net_pnl, "costs": self.costs,
            "shadow": self.shadow, "suppressed": self.suppressed, "grade": self.grade, "score": self.score,
            "note": self.note, "cash_fut": self.cash_fut, "tags": self.tags,
            "candles_since_entry": self.candles_since_entry,
        }


class TradeManager:
    def __init__(self, config: Callable[[], AppConfig], on_closed: Callable[[Trade, float], list[Event]]):
        self._config = config
        self._on_closed = on_closed
        self.trades: dict[str, Trade] = {}
        self.active: dict[int, Trade] = {}
        self.book = RiskBook()

    # ------------------------------------------------------------------ helpers
    def open_positions(self) -> int:
        return sum(1 for t in self.active.values() if not t.shadow)

    def _event(self, tr: Trade, ts: float, reason: str, alert: str | None, values: dict) -> Event:
        return Event(kind="trade", ts=ts, token=tr.token, symbol=tr.symbol, display=tr.display,
                     setup_id=tr.setup_id, reason=reason, alert=None if tr.shadow else alert,
                     grade=tr.grade, score=tr.score, direction=tr.direction,
                     values={"trade_id": tr.id, "shadow": tr.shadow, **values})

    # ------------------------------------------------------------------ open
    def open(self, *, setup_id: str, plan: TradePlan, inst: "Instrument", ts: float, candle_start: float,
             s2_high_view: float, s1_turnover: float, shadow: bool, suppressed: str | None, grade: str,
             score: float, params: Params, cash_fut: bool, tags: list[str]) -> tuple[Trade, list[Event]]:
        budget = params.capital * params.risk_per_trade_pct / 100.0
        tr = Trade(
            id=f"{setup_id}-T", setup_id=setup_id, token=inst.token, symbol=inst.symbol, display=inst.display,
            kind=inst.kind.value, is_future=inst.is_future, direction=plan.direction, tick=inst.tick_size or 0.05,
            lot_size=inst.lot_size, entry_ts=ts, entry=plan.entry, stop=plan.stop, initial_stop=plan.stop,
            R=plan.R, qty=plan.qty, t1=plan.t1, t2=plan.t2, s2_high=s2_high_view, s1_turnover=s1_turnover,
            entry_candle_start=candle_start, planned_entry=plan.entry,
            status="pending" if plan.entry_mode == "buy_stop" else "open",
            shadow=shadow, suppressed=suppressed, grade=grade, score=score, last=plan.entry,
            stat_qty=plan.qty if plan.qty > 0 else (max(1, int(budget // plan.R)) if plan.R > 0 else 1),
            note=plan.note, cash_fut=cash_fut, tags=list(tags),
        )
        self.trades[tr.id] = tr
        self.active[tr.token] = tr
        ev = self._event(tr, ts, "opened", None, {"plan": plan.as_dict(), "status": tr.status})
        return tr, [ev]

    # ------------------------------------------------------------------ prices
    def on_price(self, token: int, price_real: float, ts: float, params: Params) -> list[Event]:
        tr = self.active.get(token)
        if tr is None:
            return []
        return self._price(tr, tr.direction * price_real, ts, params, continuous=False)

    def _price(self, tr: Trade, p: float, ts: float, params: Params, continuous: bool) -> list[Event]:
        ev: list[Event] = []
        if tr.status == "pending":
            if p < tr.entry:
                return ev
            fill = tr.entry if continuous else max(p, tr.entry)
            tr.entry = fill
            tr.R = tr.entry - tr.initial_stop
            tr.status = "open"
            tr.entry_ts = ts
            ev.append(self._event(tr, ts, "filled", None, {"fill": tr.real(fill)}))
        tr.last = p
        move = p - tr.entry
        if move > tr.mfe:
            tr.mfe = move
        if move < tr.mae:
            tr.mae = move
        if p <= tr.stop:
            fill = tr.stop if continuous else p
            if tr.status == "open":
                reason = "stop"
            elif tr.stop <= tr.entry + 1e-9:
                reason = "breakeven_stop"
            else:
                reason = "trail_stop"
            ev += self._exit(tr, tr.fraction_open, fill, ts, reason, params)
            return ev
        if tr.status == "open" and p >= tr.t1:
            frac = min(params.t1_book_fraction, tr.fraction_open)
            ev += self._exit(tr, frac, tr.t1, ts, "t1", params, alert=AlertType.T1)
            if tr.status != "closed":
                tr.status = "t1"
                tr.stop = max(tr.stop, tr.entry)
        if tr.status == "t1" and params.t2_mode == "target" and p >= tr.t2:
            ev += self._exit(tr, tr.fraction_open, tr.t2, ts, "t2", params)
        return ev

    # ------------------------------------------------------------------ candles
    def on_candle(self, c: Candle, hist_view: list[Candle], params: Params, session: "SessionRules",
                  mode: str, close_ts: float) -> list[Event]:
        tr = self.active.get(c.token)
        if tr is None or c.start <= tr.entry_candle_start:
            return []
        vc = view(c, tr.direction)
        ev: list[Event] = []
        if mode == "ohlc":
            pts = [vc.open, vc.low, vc.high, vc.close] if vc.close >= vc.open else [vc.open, vc.high, vc.low, vc.close]
            for i, p in enumerate(pts):
                ev += self._price(tr, p, c.start + 15.0 * i, params, continuous=i > 0)
                if not tr.is_active:
                    return ev
        if tr.status == "pending":
            tr.pending_candles += 1
            if tr.pending_candles >= params.buy_stop_valid_candles:
                tr.status = "cancelled"
                tr.exit_reason = "buy_stop_not_triggered"
                tr.closed_ts = close_ts
                del self.active[tr.token]
                ev.append(self._event(tr, close_ts, "cancelled", None, {"reason": tr.exit_reason}))
                ev += self._on_closed(tr, close_ts)
            return ev
        if not tr.is_active:
            return ev
        tr.candles_since_entry += 1
        if vc.high > tr.s2_high:
            tr.new_high = True
        end_min = minute_of_day(c.end)
        kind = Kind(tr.kind)
        if is_sell_surge(vc, tr.s1_turnover, params):
            ev.append(self._event(tr, close_ts, "exit_warning", AlertType.EXIT_WARNING,
                                  {"turnover_cr": vc.turnover_cr, "sell_share": vc.sell_share,
                                   "turnover_vs_s1": vc.turnover_cr / tr.s1_turnover}))
            ev += self._exit(tr, tr.fraction_open, vc.close, close_ts, "sell_surge", params)
        elif tr.candles_since_entry >= params.time_stop_candles and not tr.new_high:
            ev += self._exit(tr, tr.fraction_open, vc.close, close_ts, "time_stop", params)
        elif end_min >= session.eod_exit(kind):
            ev += self._exit(tr, tr.fraction_open, vc.close, close_ts, "eod", params)
        elif tr.status == "t1":
            n = max(1, params.trail_candle_minutes)
            m = minute_of_day(c.start)
            if (m - session.cont_start) % n == n - 1 and len(hist_view) >= n:
                bucket = hist_view[-n:]
                if bucket[-1].start == c.start:
                    tr.trail_low = min(x.low for x in bucket)
            levels = [x for x in (tr.trail_low, vc.vwap) if x is not None]
            if levels:
                new_stop = round_down(max(levels) - tr.tick, tr.tick)
                if new_stop > tr.stop:
                    old = tr.stop
                    tr.stop = new_stop
                    ev.append(self._event(tr, close_ts, "stop_trailed", None,
                                          {"from": tr.real(old), "to": tr.real(new_stop)}))
        return ev

    def provisional_exit_warning(self, c: Candle, params: Params, ts: float) -> list[Event]:
        """Mid-candle sell-side surge on an open trade (alert only)."""
        tr = self.active.get(c.token)
        if tr is None or not tr.is_active or tr.status == "pending" or c.start <= tr.entry_candle_start:
            return []
        if tr.warned_minute == c.start:
            return []
        vc = view(c, tr.direction)
        T = vc.running_turnover_cr
        if (T >= params.exit_warning_turnover_ratio * tr.s1_turnover and vc.close < vc.open
                and vc.sell_share >= params.exit_warning_sell_share_min and vc.tradeable):
            tr.warned_minute = c.start
            ev = self._event(tr, ts, "exit_warning", AlertType.EXIT_WARNING,
                             {"turnover_cr": T, "sell_share": vc.sell_share})
            ev.provisional = True
            return [ev]
        return []

    def force_exit(self, token: int, price_real: float, ts: float, reason: str, params: Params) -> list[Event]:
        tr = self.active.get(token)
        if tr is None:
            return []
        if tr.status == "pending":
            tr.status = "cancelled"
            tr.exit_reason = reason
            tr.closed_ts = ts
            del self.active[token]
            return [self._event(tr, ts, "cancelled", None, {"reason": reason})] + self._on_closed(tr, ts)
        return self._exit(tr, tr.fraction_open, tr.direction * price_real, ts, reason, params)

    # ------------------------------------------------------------------ exits
    def _exit(self, tr: Trade, fraction: float, price: float, ts: float, reason: str, params: Params,
              alert: str | None = AlertType.EXIT) -> list[Event]:
        fraction = min(fraction, tr.fraction_open)
        if fraction <= 0:
            return []
        tr.exits.append({"ts": ts, "price": price, "fraction": round(fraction, 6), "reason": reason})
        tr.fraction_open = round(tr.fraction_open - fraction, 9)
        ev = [self._event(tr, ts, reason, alert,
                          {"price": tr.real(price), "fraction": fraction, "r": (price - tr.entry) / tr.R if tr.R else None,
                           "fraction_open": tr.fraction_open})]
        if tr.fraction_open <= 1e-9:
            ev += self._close(tr, ts, reason, params)
        return ev

    def _close(self, tr: Trade, ts: float, reason: str, params: Params) -> list[Event]:
        tr.status = "closed"
        tr.closed_ts = ts
        tr.exit_reason = reason
        self.active.pop(tr.token, None)
        self._settle(tr)
        if not tr.shadow and tr.qty > 0:
            self.book.record(tr.net_pnl or 0.0)
        ev = [self._event(tr, ts, "closed", None,
                          {"exit_reason": reason, "gross_r": tr.gross_r, "net_r": tr.net_r,
                           "gross_pnl": tr.gross_pnl, "net_pnl": tr.net_pnl, "costs": tr.costs,
                           "mfe_r": tr.mfe / tr.R if tr.R else None, "mae_r": tr.mae / tr.R if tr.R else None})]
        return ev + self._on_closed(tr, ts)

    def _settle(self, tr: Trade) -> None:
        cfg = self._config()
        bt = cfg.backtest
        table = bt.costs.get("FUT" if tr.is_future else "EQ") or next(iter(bt.costs.values()))
        d = tr.direction
        q = tr.qty if tr.qty > 0 else tr.stat_qty
        side_in, side_out = ("buy", "sell") if d > 0 else ("sell", "buy")
        e_real = d * tr.entry
        e_slip = d * slipped(e_real, side_in, tr.is_future, tr.tick, bt)
        gross_unit = 0.0
        net_unit = 0.0
        fills = [(side_in, abs(e_real) * q)]
        for x in tr.exits:
            f = x["fraction"]
            x_real = d * x["price"]
            x_slip = d * slipped(x_real, side_out, tr.is_future, tr.tick, bt)
            gross_unit += f * (x["price"] - tr.entry)
            net_unit += f * (x_slip - e_slip)
            fills.append((side_out, abs(x_real) * f * q))
        costs = fill_costs(fills, table)
        tr.costs = round(costs, 2)
        tr.gross_pnl = round(gross_unit * q, 2)
        tr.net_pnl = round(net_unit * q - costs, 2)
        tr.gross_r = round(gross_unit / tr.R, 4) if tr.R else None
        tr.net_r = round((net_unit * q - costs) / (tr.R * q), 4) if tr.R and q else None
