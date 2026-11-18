"""Small streaming indicators."""

from __future__ import annotations


class WilderATR:
    """ATR on 1-minute candles with Wilder smoothing.

    ``seed`` (from the previous session, built by the baseline job) makes ATR
    available from the first candle. The first candle of a session uses
    high - low as its true range (the overnight gap is ignored).
    """

    __slots__ = ("period", "value", "_trs", "prev_close")

    def __init__(self, period: int = 14, seed: float | None = None):
        self.period = period
        self.value: float | None = seed if seed and seed > 0 else None
        self._trs: list[float] = []
        self.prev_close: float | None = None

    def update(self, high: float, low: float, close: float) -> float | None:
        if self.prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self.prev_close), abs(low - self.prev_close))
        if self.value is None:
            self._trs.append(tr)
            if len(self._trs) >= self.period:
                self.value = sum(self._trs[-self.period:]) / self.period
        else:
            self.value = (self.value * (self.period - 1) + tr) / self.period
        self.prev_close = close
        return self.current

    @property
    def current(self) -> float | None:
        if self.value is not None:
            return self.value
        if self._trs:
            return sum(self._trs) / len(self._trs)
        return None


def atr_of(bars: list[tuple[float, float, float]], period: int = 14) -> float | None:
    """ATR after a sequence of (high, low, close) bars."""
    a = WilderATR(period)
    for h, l, c in bars:
        a.update(h, l, c)
    return a.current
