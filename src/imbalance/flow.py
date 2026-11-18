"""Estimated buy/sell split of volume increments between snapshots (spec §3).

Quote rule first (trade at or above the previous best ask = buy, at or below the
previous best bid = sell), then the tick rule (up = buy, down = sell, unchanged =
same side as last). Snapshot feeds blur this; the adapter interface allows a
tick-by-tick vendor to be swapped in.
"""

from __future__ import annotations

BUY = 1
SELL = -1
UNKNOWN = 0


def classify(ltp: float, prev_ltp: float, prev_bid: float, prev_ask: float, last_side: int) -> int:
    if prev_ask > 0 and ltp >= prev_ask:
        return BUY
    if prev_bid > 0 and ltp <= prev_bid:
        return SELL
    if prev_ltp > 0:
        if ltp > prev_ltp:
            return BUY
        if ltp < prev_ltp:
            return SELL
    return last_side


def book_pressure(total_buy_qty: int, total_sell_qty: int) -> float | None:
    if total_sell_qty <= 0:
        return None
    return total_buy_qty / total_sell_qty
