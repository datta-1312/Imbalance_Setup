"""Trade plan (stop, risk per share, size, targets) and daily guardrails (§7)."""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..config import Params
from ..models import Instrument


def round_down(x: float, tick: float) -> float:
    return round(math.floor(x / tick + 1e-9) * tick, 6)


def round_up(x: float, tick: float) -> float:
    return round(math.ceil(x / tick - 1e-9) * tick, 6)


def round_nearest(x: float, tick: float) -> float:
    return round(math.floor(x / tick + 0.5 + 1e-9) * tick, 6)


@dataclass(slots=True)
class TradePlan:
    """Prices are in the setup's view space (negated for shorts)."""

    direction: int
    entry: float
    stop: float
    R: float
    risk_pct: float
    qty: int
    lots: int | None
    t1: float
    t2: float
    position_value: float
    entry_mode: str
    note: str | None = None
    stop_basis: float = 0.0
    atr: float | None = None

    def real(self, x: float) -> float:
        return round(self.direction * x, 6)

    def as_dict(self) -> dict:
        return {"direction": self.direction, "entry": self.real(self.entry), "stop": self.real(self.stop),
                "R": round(self.R, 6), "risk_pct": round(self.risk_pct, 4), "qty": self.qty, "lots": self.lots,
                "t1": self.real(self.t1), "t2": self.real(self.t2),
                "position_value": round(self.position_value, 2), "entry_mode": self.entry_mode,
                "note": self.note, "stop_basis": self.real(self.stop_basis), "atr": self.atr}


def compute_plan(entry: float, pullback_low: float, s1_low: float, atr: float | None, params: Params,
                 inst: Instrument, direction: int) -> TradePlan:
    tick = inst.tick_size or 0.05
    basis = pullback_low if params.stop_mode == "pullback_low" else s1_low
    buffer = params.stop_buffer_atr * (atr or 0.0)
    stop = round_down(basis - buffer, tick)
    R = entry - stop
    risk_pct = (R / abs(entry) * 100.0) if entry else 0.0
    budget = params.capital * params.risk_per_trade_pct / 100.0
    qty = int(budget // R) if R > 0 else 0
    cap_value = params.max_position_value_x_capital * params.capital
    if entry:
        qty = min(qty, int(cap_value // abs(entry)))
    lots = None
    note = None
    if inst.is_future:
        lot = max(1, inst.lot_size)
        lots = qty // lot
        qty = lots * lot
        if lots == 0:
            note = "risk budget is below one lot"
    t1 = round_nearest(entry + params.t1_r * R, tick)
    t2 = round_nearest(entry + params.t2_r * R, tick)
    return TradePlan(direction=direction, entry=entry, stop=stop, R=R, risk_pct=risk_pct, qty=qty, lots=lots,
                     t1=t1, t2=t2, position_value=qty * abs(entry), entry_mode=params.entry_mode, note=note,
                     stop_basis=basis, atr=atr)


class RiskBook:
    """Daily guardrails: after N losing trades or a daily loss limit, stop
    raising entry alerts (they are still logged); cap open positions."""

    def __init__(self) -> None:
        self.realized = 0.0
        self.losers = 0
        self.winners = 0
        self.closed = 0

    def record(self, net_pnl: float) -> None:
        self.closed += 1
        self.realized += net_pnl
        if net_pnl < 0:
            self.losers += 1
        else:
            self.winners += 1

    def tripped(self, params: Params) -> str | None:
        if self.losers >= params.max_daily_losses:
            return "max_daily_losses"
        if self.realized <= -params.max_daily_loss_pct / 100.0 * params.capital:
            return "max_daily_loss"
        return None

    def as_dict(self, params: Params, open_positions: int) -> dict:
        return {"realized_pnl": round(self.realized, 2), "losers": self.losers, "winners": self.winners,
                "closed": self.closed, "open_positions": open_positions,
                "max_open_positions": params.max_open_positions, "tripped": self.tripped(params),
                "daily_loss_pct": round(-self.realized / params.capital * 100.0, 3) if params.capital else 0.0}
