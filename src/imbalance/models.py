"""Core data types shared by every component."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import IntFlag, StrEnum

from .timeutil import minute_of_day


class Kind(StrEnum):
    EQ = "EQ"     # cash share
    FUT = "FUT"   # stock future
    IDX = "IDX"   # index future (market-regime reading only, never a setup)


@dataclass(slots=True)
class Instrument:
    token: int
    symbol: str                 # broker trading symbol, e.g. TATASTEEL or TATASTEEL26NOVFUT
    underlying: str             # e.g. TATASTEEL
    kind: Kind
    exchange: str = "NSE"
    lot_size: int = 1
    tick_size: float = 0.05
    expiry: date | None = None
    series: str = ""            # "", "current" or "next" for futures
    lower_band: float | None = None
    upper_band: float | None = None
    adtv_cr: float | None = None
    linked_token: int | None = None   # cash <-> current-month future of the same underlying
    scannable: bool = True
    scan_block_reason: str | None = None
    banned: bool = False
    rollover: bool = False       # inside the last N sessions before this future's expiry

    @property
    def display(self) -> str:
        if self.kind == Kind.EQ:
            return self.underlying
        if self.series == "next":
            return f"{self.underlying} FUT-NEXT"
        return f"{self.underlying} FUT"

    @property
    def is_future(self) -> bool:
        return self.kind in (Kind.FUT, Kind.IDX)


@dataclass(slots=True)
class Tick:
    """One broker snapshot (not a trade). Cumulative fields drive turnover."""

    token: int
    ts: float                    # exchange timestamp (epoch seconds)
    ltp: float
    volume: int                  # volume traded today (units), cumulative
    atp: float                   # average traded price today
    recv_ts: float = 0.0         # local receive time (epoch seconds)
    last_qty: int = 0
    oi: int | None = None
    bid_px: tuple[float, ...] = ()
    bid_qty: tuple[int, ...] = ()
    ask_px: tuple[float, ...] = ()
    ask_qty: tuple[int, ...] = ()
    total_buy_qty: int = 0
    total_sell_qty: int = 0
    day_open: float = 0.0
    day_high: float = 0.0
    day_low: float = 0.0
    prev_close: float = 0.0      # previous session close ("close" field of the feed)
    last_trade_ts: float = 0.0

    @property
    def best_bid(self) -> float:
        return self.bid_px[0] if self.bid_px else 0.0

    @property
    def best_ask(self) -> float:
        return self.ask_px[0] if self.ask_px else 0.0


class Flag(IntFlag):
    NONE = 0
    GAP = 1            # overlaps a feed gap / stale period: cannot trigger a surge
    FALLBACK = 2       # turnover from ΔV x mid instead of cumulative ATP x V
    NO_DATA = 4        # no snapshot during the minute
    PARTIAL = 8        # first snapshot of the day landed here (baseline only)
    BLOCK_WINDOW = 16  # inside the 14:05–14:20 block-deal window
    LATE = 32          # received snapshots stamped in an earlier minute
    ANOMALY = 64       # cumulative volume went backwards (feed correction)


UNTRADEABLE = Flag.GAP | Flag.NO_DATA | Flag.PARTIAL


@dataclass(slots=True)
class Candle:
    token: int
    start: float                 # epoch of minute start (exchange time)
    open: float
    high: float
    low: float
    close: float
    turnover_cr: float = 0.0     # final turnover (₹ crore), set at close
    volume: int = 0
    buy_vol: int = 0
    sell_vol: int = 0
    unk_vol: int = 0
    vwap: float | None = None    # day's ATP at the last snapshot
    oi_open: int | None = None
    oi_close: int | None = None
    n_ticks: int = 0
    flags: Flag = Flag.NONE
    total_buy_qty: int = 0
    total_sell_qty: int = 0
    bid5_qty: int = 0
    ask5_qty: int = 0
    prev_day_close: float | None = None
    # cumulative bookkeeping (rupees / units)
    ct_start: float = 0.0
    ct_end: float = 0.0
    v_start: int = 0
    v_end: int = 0
    fallback_rupees: float = 0.0
    ref_price: float = 0.0       # last price before the candle started
    closed: bool = False
    meta: dict = field(default_factory=dict)

    @property
    def end(self) -> float:
        return self.start + 60.0

    @property
    def minute(self) -> int:
        return minute_of_day(self.start)

    @property
    def running_turnover_cr(self) -> float:
        raw = self.ct_end - self.ct_start
        if raw < 0:
            raw = self.fallback_rupees
        return raw / 1e7

    @property
    def turnover(self) -> float:
        """Final turnover if closed, running turnover otherwise (₹ crore)."""
        return self.turnover_cr if self.closed else self.running_turnover_cr

    @property
    def classified_vol(self) -> int:
        return self.buy_vol + self.sell_vol + self.unk_vol

    @property
    def buy_share(self) -> float:
        tot = max(self.volume, self.classified_vol)
        return min(1.0, self.buy_vol / tot) if tot > 0 else 0.0

    @property
    def sell_share(self) -> float:
        tot = max(self.volume, self.classified_vol)
        return min(1.0, self.sell_vol / tot) if tot > 0 else 0.0

    @property
    def green(self) -> bool:
        return self.close > self.open

    @property
    def red(self) -> bool:
        return self.close < self.open

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def close_location(self) -> float:
        """0 = close at the low, 1 = close at the high."""
        rng = self.high - self.low
        if rng <= 0:
            return 0.5
        return (self.close - self.low) / rng

    @property
    def tradeable(self) -> bool:
        return not (self.flags & UNTRADEABLE)

    def copy(self) -> "Candle":
        c = Candle(**{f: getattr(self, f) for f in self.__dataclass_fields__ if f != "meta"})
        c.meta = dict(self.meta)
        return c


def mirror(c: Candle) -> Candle:
    """Price-negated view of a candle with buy and sell volume swapped.

    The short setup (§5.7) is the long setup applied to this view, so one code
    path implements both directions. Real price = direction x view price.
    """
    m = Candle(
        token=c.token, start=c.start,
        open=-c.open, high=-c.low, low=-c.high, close=-c.close,
        turnover_cr=c.turnover_cr, volume=c.volume,
        buy_vol=c.sell_vol, sell_vol=c.buy_vol, unk_vol=c.unk_vol,
        vwap=(-c.vwap if c.vwap is not None else None),
        oi_open=c.oi_open, oi_close=c.oi_close, n_ticks=c.n_ticks, flags=c.flags,
        total_buy_qty=c.total_sell_qty, total_sell_qty=c.total_buy_qty,
        bid5_qty=c.ask5_qty, ask5_qty=c.bid5_qty,
        prev_day_close=(-c.prev_day_close if c.prev_day_close else c.prev_day_close),
        ct_start=c.ct_start, ct_end=c.ct_end, v_start=c.v_start, v_end=c.v_end,
        fallback_rupees=c.fallback_rupees, ref_price=-c.ref_price, closed=c.closed,
    )
    m.meta = c.meta
    return m


def view(c: Candle, direction: int) -> Candle:
    return c if direction > 0 else mirror(c)
