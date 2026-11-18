"""Transaction costs and slippage (spec §11). All rates come from the config's
cost table; the defaults are placeholders to confirm against a contract note."""

from __future__ import annotations

from typing import Iterable

from .config import BacktestConfig, CostTable


def fill_costs(fills: Iterable[tuple[str, float]], table: CostTable) -> float:
    """Costs for a list of executed orders ``(side, value_in_rupees)``.

    Brokerage is charged per order (percentage, capped); STT and stamp duty
    depend on the side; exchange and SEBI charges on all turnover; GST on
    brokerage + exchange + SEBI charges.
    """
    brokerage = stt = exch = sebi = stamp = 0.0
    for side, value in fills:
        value = abs(value)
        if value <= 0:
            continue
        brokerage += min(table.brokerage_pct / 100.0 * value, table.brokerage_max_per_order)
        exch += table.exchange_txn_pct / 100.0 * value
        sebi += table.sebi_fee_pct / 100.0 * value
        if side == "buy":
            stt += table.stt_buy_pct / 100.0 * value
            stamp += table.stamp_buy_pct / 100.0 * value
        else:
            stt += table.stt_sell_pct / 100.0 * value
    gst = table.gst_pct / 100.0 * (brokerage + exch + sebi)
    return brokerage + stt + exch + sebi + stamp + gst


def slipped(price: float, side: str, is_future: bool, tick: float, cfg: BacktestConfig) -> float:
    """Fill price after slippage, always against the trader."""
    if is_future:
        adj = cfg.slippage_fut_ticks * tick + abs(price) * cfg.slippage_fut_pct / 100.0
    else:
        adj = abs(price) * cfg.slippage_cash_pct / 100.0
    return price + adj if side == "buy" else price - adj
