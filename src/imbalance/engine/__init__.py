"""Setup engine: the state machine of spec §5–§7 as deterministic functions over
the candle stream."""

from .engine import Ctx, ScreenerEngine
from .tracker import Setup, Tracker
from .trades import Trade, TradeManager
from .types import AlertType, Check, Evaluation, Event, Reason, State

__all__ = ["AlertType", "Check", "Ctx", "Evaluation", "Event", "Reason", "ScreenerEngine", "Setup", "State",
           "Trade", "TradeManager", "Tracker"]
