"""Latency and rate statistics for the health panel."""

from __future__ import annotations

from collections import deque


class LatencyStats:
    """Rolling window of durations (seconds) with percentile summaries."""

    def __init__(self, window: int = 20000):
        self._d: deque[float] = deque(maxlen=window)
        self.count = 0
        self.max = 0.0

    def add(self, seconds: float) -> None:
        self._d.append(seconds)
        self.count += 1
        if seconds > self.max:
            self.max = seconds

    def percentile(self, q: float) -> float | None:
        if not self._d:
            return None
        xs = sorted(self._d)
        i = min(len(xs) - 1, max(0, int(round(q / 100.0 * (len(xs) - 1)))))
        return xs[i]

    def summary_ms(self) -> dict:
        def ms(x: float | None) -> float | None:
            return None if x is None else round(x * 1000.0, 3)
        return {"count": self.count, "p50": ms(self.percentile(50)), "p99": ms(self.percentile(99)),
                "max": ms(self.max if self.count else None)}


class RateMeter:
    """Events per second over a sliding window of whole seconds."""

    def __init__(self, seconds: int = 5):
        self.seconds = seconds
        self._buckets: deque[tuple[int, int]] = deque()

    def add(self, now: float, n: int = 1) -> None:
        sec = int(now)
        if self._buckets and self._buckets[-1][0] == sec:
            s, c = self._buckets[-1]
            self._buckets[-1] = (s, c + n)
        else:
            self._buckets.append((sec, n))
        while self._buckets and self._buckets[0][0] <= sec - self.seconds:
            self._buckets.popleft()

    def rate(self, now: float) -> float:
        sec = int(now)
        total = sum(c for s, c in self._buckets if s > sec - self.seconds)
        return total / self.seconds
