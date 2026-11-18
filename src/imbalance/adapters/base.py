"""Pluggable market-data adapter interface (spec §8.1).

Implementations: Zerodha Kite Connect (websocket, full mode), recorded-tick
replay, and a synthetic market. A tick-by-tick vendor can be added by
implementing the same interface.
"""

from __future__ import annotations

import abc
import logging
import time
from typing import Callable, Iterable

from ..models import Tick

log = logging.getLogger(__name__)

TickCallback = Callable[[list[Tick]], None]
StatusCallback = Callable[[str, float, dict], None]   # ("connected" | "disconnected" | "reconnected" | "error", ts, info)


class MarketDataAdapter(abc.ABC):
    """``subscribe(tokens)``, ``on_tick``/``on_ticks``, ``on_disconnect`` and
    automatic reconnect with resubscribe; ``run()`` drives it until ``close()``."""

    name = "base"

    def __init__(self) -> None:
        self.on_ticks: TickCallback | None = None
        self.on_status: StatusCallback | None = None
        self.tokens: list[int] = []
        self.connected = False
        self.reconnects = 0
        self.frames = 0
        self.ticks = 0
        self.last_message_ts: float | None = None

    # -- callbacks -----------------------------------------------------------
    def on_tick(self, cb: Callable[[Tick], None]) -> None:
        """Per-tick callback convenience (wraps the batch callback)."""
        self.on_ticks = lambda ticks: [cb(t) for t in ticks]

    def emit_ticks(self, ticks: list[Tick]) -> None:
        self.frames += 1
        self.ticks += len(ticks)
        if self.on_ticks is not None and ticks:
            self.on_ticks(ticks)

    def emit_status(self, status: str, ts: float | None = None, **info) -> None:
        ts = time.time() if ts is None else ts
        log.info("%s feed status: %s %s", self.name, status, info or "")
        if self.on_status is not None:
            self.on_status(status, ts, info)

    # -- lifecycle -------------------------------------------------------------
    async def subscribe(self, tokens: Iterable[int]) -> None:
        self.tokens = sorted(set(self.tokens) | set(tokens))

    @abc.abstractmethod
    async def run(self) -> None:
        """Connect and stream until ``close()``; reconnect on failures."""

    async def close(self) -> None:  # pragma: no cover - default no-op
        return None

    def stats(self) -> dict:
        return {"adapter": self.name, "connected": self.connected, "reconnects": self.reconnects,
                "frames": self.frames, "ticks": self.ticks, "subscribed": len(self.tokens),
                "last_message_ts": self.last_message_ts}
