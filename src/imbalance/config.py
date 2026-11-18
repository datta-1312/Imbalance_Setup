"""Configuration: one YAML file with every threshold, session timing and broker
setting (spec §10). Supports named presets, per-symbol overrides, a local overlay
written by the dashboard, and hot reload without a restart.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
from datetime import date
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from .timeutil import parse_hhmm

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("config/default.yaml")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _valid_time(v: str) -> str:
    parse_hhmm(v)
    return v


# ---------------------------------------------------------------------------
# Tunable parameters (§10). Frozen so that one object can be shared and cached.
# ---------------------------------------------------------------------------
class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Surge 1 (§5.1)
    surge_turnover_floor_cr: float = 4.0
    surge_rvol_min: float = 5.0
    suddenness_multiple: float = 4.0
    suddenness_lookback_candles: int = 10
    suddenness_min_candles: int = 3
    surge_window_candles: int = Field(1, ge=1, le=2)
    surge_min_move_pct: float = 0.3
    close_in_top_pct: float = 30.0
    buy_share_min: float = 0.60
    leg_start_lookback_candles: int = 5
    opening_range_minutes: int = 15
    nifty_down_limit_pct: float = 0.5

    # Baseline (§3)
    baseline_sessions: int = 20
    baseline_smoothing_minutes: int = 2
    baseline_min_sessions: int = 10
    baseline_floor_cr: float = 0.01

    # Pullback / invalidation (§5.2, §5.4)
    pullback_min_retrace: float = 0.25
    pullback_max_retrace: float = 0.618
    pullback_floor: Literal["s1_low", "s1_midpoint"] = "s1_low"
    pullback_max_turnover_ratio: float = 0.35
    pullback_min_candles: int = 3
    pullback_max_sell_share_scored: float = 0.65
    distribution_turnover_ratio: float = 0.60
    distribution_sell_share_min: float = 0.60
    trigger_lookback_candles: int = 3

    # Surge 2 (§5.3)
    s2_turnover_vs_s1: float = 0.60
    s2_floor_cr: float = 3.0
    s2_rvol_min: float = 3.0
    entry_mode: Literal["close", "buy_stop"] = "close"
    buy_stop_valid_candles: int = 2
    extended_atr_multiple: float = 0.5

    # Expiry and cooldown (§5.5)
    setup_expiry_minutes: int = 45
    cooldown_minutes: int = 15

    # Futures OI (§6)
    oi_change_min_pct: float = 0.5
    oi_window_candles: int = 3
    oi_heavy_unwind_pct: float = 1.5
    short_buildup_turnover_ratio: float = 0.35

    # Session windows (§4)
    no_signal_before: str = "09:20"
    last_surge1_time: str = "15:00"
    last_entry_time: str = "15:05"

    # Universe filters (§4)
    adtv_floor_cr: float = 25.0
    price_band_proximity_pct: float = 1.0
    stale_after_seconds: float = 30.0

    # Risk and exits (§7)
    atr_period: int = 14
    stop_mode: Literal["pullback_low", "s1_low"] = "pullback_low"
    stop_buffer_atr: float = 0.25
    min_risk_pct: float = 0.15
    max_risk_pct: float = 1.2
    capital: float = 1_000_000.0
    risk_per_trade_pct: float = 0.5
    max_position_value_x_capital: float = 2.0
    t1_r: float = 1.5
    t2_r: float = 3.0
    t1_book_fraction: float = 0.5
    t2_mode: Literal["target", "trail"] = "target"
    trail_candle_minutes: int = 3
    time_stop_candles: int = 10
    exit_warning_turnover_ratio: float = 0.60
    exit_warning_sell_share_min: float = 0.60
    max_daily_losses: int = 3
    max_daily_loss_pct: float = 1.5
    max_open_positions: int = 3

    # Grade (§5.6)
    grade_a_min: float = 75
    grade_b_min: float = 60

    # Event filters (§4)
    event_day_mode: Literal["tag", "suppress", "require_a"] = "tag"
    rollover_sessions: int = 5
    rollover_requires_cash_confirmation: bool = True
    scan_special_sessions: bool = False

    # Direction
    short_setup_enabled: bool = False
    direction_mode: Literal["flow", "candle"] = "flow"

    @field_validator("no_signal_before", "last_surge1_time", "last_entry_time")
    @classmethod
    def _times(cls, v: str) -> str:
        return _valid_time(v)

    # Parsed minute-of-day helpers (parse_hhmm is cached).
    @property
    def no_signal_before_min(self) -> int:
        return parse_hhmm(self.no_signal_before)

    @property
    def last_surge1_min(self) -> int:
        return parse_hhmm(self.last_surge1_time)

    @property
    def last_entry_min(self) -> int:
        return parse_hhmm(self.last_entry_time)


# ---------------------------------------------------------------------------
# Other sections
# ---------------------------------------------------------------------------
class SessionConfig(_Strict):
    block_deal_1: tuple[str, str] = ("08:45", "09:00")
    pre_open: tuple[str, str] = ("09:00", "09:15")
    continuous_start: str = "09:15"
    block_deal_2: tuple[str, str] = ("14:05", "14:20")
    cash_continuous_end: str = "15:15"
    cash_closing_auction: tuple[str, str] = ("15:15", "15:35")
    futures_end: str = "15:40"
    eod_exit_cash: str = "15:10"
    eod_exit_fut: str = "15:25"
    candle_close_grace_seconds: float = 1.5
    startup_time: str = "08:30"
    shutdown_time: str = "15:50"

    @model_validator(mode="after")
    def _check(self) -> "SessionConfig":
        for name in ("continuous_start", "cash_continuous_end", "futures_end", "eod_exit_cash",
                     "eod_exit_fut", "startup_time", "shutdown_time"):
            _valid_time(getattr(self, name))
        for name in ("block_deal_1", "pre_open", "block_deal_2", "cash_closing_auction"):
            a, b = getattr(self, name)
            if parse_hhmm(a) > parse_hhmm(b):
                raise ValueError(f"session.{name}: start after end")
        return self


class SpecialSession(_Strict):
    date: date
    name: str
    start: str
    end: str


class CalendarConfig(_Strict):
    weekend_days: list[int] = [5, 6]
    holidays: list[date] = []
    holidays_file: str | None = None
    special_sessions: list[SpecialSession] = []
    events_file: str | None = None


class UniverseConfig(_Strict):
    index_underlyings: list[str] = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50"]
    regime_underlyings: list[str] = ["NIFTY", "BANKNIFTY"]
    ban_list_url: str | None = None
    ban_list_file: str | None = None
    include_next_month_in_rollover: bool = True
    symbols_allowlist: list[str] = []


class KiteConfig(_Strict):
    api_key_env: str = "KITE_API_KEY"
    api_secret_env: str = "KITE_API_SECRET"
    session_file: str = "data/secrets/kite_session.json"
    ws_url: str = "wss://ws.kite.trade"
    api_root: str = "https://api.kite.trade"
    login_url: str = "https://kite.zerodha.com/connect/login"
    max_instruments_per_connection: int = 3000
    max_connections: int = 3
    futures_volume_unit: Literal["units", "lots"] = "units"
    heartbeat_timeout_seconds: float = 5.0
    reconnect_initial_delay_seconds: float = 1.0
    reconnect_max_delay_seconds: float = 30.0
    historical_requests_per_second: float = 3.0
    quote_batch_size: int = 500


class BrokerConfig(_Strict):
    adapter: Literal["kite", "replay", "synthetic"] = "kite"
    kite: KiteConfig = KiteConfig()


class TelegramConfig(_Strict):
    enabled: bool = False
    bot_token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id_env: str = "TELEGRAM_CHAT_ID"
    api_base: str = "https://api.telegram.org"
    alert_types: list[str] = ["S1_CONFIRMED", "ARMED", "S2_PROVISIONAL", "ENTRY", "T1", "EXIT",
                              "EXIT_WARNING", "SYSTEM"]
    min_grade: Literal["A", "B", "C"] = "B"
    min_interval_seconds: float = 1.0


class AlertsConfig(_Strict):
    dedupe_window_seconds: float = 120.0
    sounds: bool = True
    telegram: TelegramConfig = TelegramConfig()


class StorageConfig(_Strict):
    data_dir: str = "data"
    record_raw_ticks: bool = True
    raw_rotate_minutes: int = 10
    raw_flush_seconds: float = 5.0
    raw_compression: str = "zstd"
    candle_backend: Literal["sqlite", "postgres", "none"] = "sqlite"
    sqlite_path: str = "data/candles.sqlite"
    postgres_dsn_env: str = "DATABASE_URL"
    event_log_dir: str = "data/events"
    baseline_dir: str = "data/baselines"


class BusConfig(_Strict):
    backend: Literal["memory", "redis"] = "memory"
    redis_url_env: str = "REDIS_URL"
    channel_prefix: str = "imbalance"


class ApiConfig(_Strict):
    host: str = "0.0.0.0"
    port: int = 8000
    static_dir: str = "dashboard/dist"
    snapshot_interval_seconds: float = 1.0
    auth_token_env: str | None = "IMBALANCE_API_TOKEN"


class GradingConfig(_Strict):
    weights: dict[str, float] = {
        "rvol": 18, "buy_share": 14, "vwap": 10, "breakout": 5, "oi": 14, "cash_fut": 10,
        "dryup": 14, "pullback_sell": 5, "regime": 5, "book": 5,
    }
    penalties: dict[str, float] = {
        "block_window": 10, "event_day": 10, "index_event": 10, "extended": 10,
        "short_covering": 10, "heavy_unwind": 5,
    }
    rvol_full_multiple: float = 3.0
    buy_share_full: float = 0.85
    book_pressure_full: float = 2.0


class CostTable(_Strict):
    brokerage_pct: float = 0.03
    brokerage_max_per_order: float = 20.0
    stt_buy_pct: float = 0.0
    stt_sell_pct: float = 0.025
    exchange_txn_pct: float = 0.00297
    sebi_fee_pct: float = 0.0001
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0


class WalkForwardConfig(_Strict):
    train_months: int = 6
    test_months: int = 2
    min_trades: int = 20
    grid: dict[str, list[float]] = {}


class BacktestConfig(_Strict):
    slippage_cash_pct: float = 0.05
    slippage_fut_ticks: int = 1
    slippage_fut_pct: float = 0.03
    random_seed: int = 7
    random_entries_per_signal: int = 3
    costs: dict[str, CostTable] = {
        "EQ": CostTable(),
        "FUT": CostTable(stt_sell_pct=0.02, exchange_txn_pct=0.00173, stamp_buy_pct=0.002),
    }
    walk_forward: WalkForwardConfig = WalkForwardConfig()


class AppConfig(_Strict):
    params: Params = Params()
    presets: dict[str, dict[str, Any]] = {}
    active_preset: str | None = None
    symbol_overrides: dict[str, dict[str, Any]] = {}
    session: SessionConfig = SessionConfig()
    calendar: CalendarConfig = CalendarConfig()
    universe: UniverseConfig = UniverseConfig()
    broker: BrokerConfig = BrokerConfig()
    alerts: AlertsConfig = AlertsConfig()
    storage: StorageConfig = StorageConfig()
    bus: BusConfig = BusConfig()
    api: ApiConfig = ApiConfig()
    grading: GradingConfig = GradingConfig()
    backtest: BacktestConfig = BacktestConfig()

    _params_cache: dict[tuple[str, str], Params] = PrivateAttr(default_factory=dict)
    _base_params: Params | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _validate_overrides(self) -> "AppConfig":
        if self.active_preset is not None and self.active_preset not in self.presets:
            raise ValueError(f"active_preset {self.active_preset!r} is not defined in presets")
        fields = set(Params.model_fields)
        for name, patch in {**self.presets, **self.symbol_overrides}.items():
            unknown = set(patch) - fields
            if unknown:
                raise ValueError(f"override {name!r} has unknown parameters: {sorted(unknown)}")
        # Validate every merged combination once, so bad values fail at load time.
        # (Private attributes are not initialised yet inside validators.)
        base = self.params
        for patch in self.presets.values():
            _merge_params(base, patch)
        if self.active_preset:
            base = _merge_params(base, self.presets[self.active_preset])
        for patch in self.symbol_overrides.values():
            _merge_params(base, patch)
        return self

    def base_params(self) -> Params:
        """Defaults with the active preset applied (no symbol overrides)."""
        if self._base_params is None:
            p = self.params
            if self.active_preset:
                p = _merge_params(p, self.presets[self.active_preset])
            self._base_params = p
        return self._base_params

    def params_for(self, symbol: str, kind: str) -> Params:
        """Effective parameters: defaults <- preset <- SYMBOL <- SYMBOL:KIND."""
        key = (symbol, kind)
        cached = self._params_cache.get(key)
        if cached is not None:
            return cached
        p = self.base_params()
        for k in (symbol, f"{symbol}:{kind}"):
            patch = self.symbol_overrides.get(k)
            if patch:
                p = _merge_params(p, patch)
        self._params_cache[key] = p
        return p

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None = None) -> "AppConfig":
        return cls.model_validate(data or {})

    def canonical(self) -> dict[str, Any]:
        return json.loads(self.model_dump_json())

    def version(self) -> str:
        blob = json.dumps(self.canonical(), sort_keys=True, default=str).encode()
        return hashlib.sha1(blob).hexdigest()[:10]


def _merge_params(base: Params, patch: dict[str, Any]) -> Params:
    data = base.model_dump()
    data.update(patch)
    return Params.model_validate(data)


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _read_yaml(path: Path, _depth: int = 0) -> dict[str, Any]:
    """Read a YAML mapping. ``extends: other.yaml`` (relative to this file) merges
    this file on top of another, e.g. a deployment file over default.yaml."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    parent = data.pop("extends", None)
    if parent:
        if _depth > 5:
            raise ValueError(f"{path}: 'extends' chain too deep")
        data = deep_merge(_read_yaml(path.parent / parent, _depth + 1), data)
    return data


class ConfigManager:
    """Loads the YAML config (+ optional local overlay) and hot-reloads it.

    The engine asks for ``params_for(symbol, kind)`` at every evaluation, so a
    reload takes effect on the next candle without a restart. Replays and
    backtests pass ``watch=False`` semantics simply by never calling ``watch``.
    """

    def __init__(self, path: str | Path | None = None, overlay: str | Path | None | Literal["auto"] = "auto",
                 data: dict[str, Any] | None = None):
        env_path = os.environ.get("IMBALANCE_CONFIG")
        self.path: Path | None = Path(path) if path else (Path(env_path) if env_path else None)
        if self.path is None and data is None and DEFAULT_CONFIG_PATH.exists():
            self.path = DEFAULT_CONFIG_PATH
        if overlay == "auto":
            self.overlay: Path | None = (self.path.parent / "local.yaml") if self.path else None
        else:
            self.overlay = Path(overlay) if overlay else None
        self._inline = data
        self._listeners: list[Callable[[AppConfig], None]] = []
        self._mtimes: tuple[float, float] = (0.0, 0.0)
        self._config = self._load()
        self._version = self._config.version()

    # -- loading -----------------------------------------------------------
    def _raw(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if self.path is not None:
            raw = _read_yaml(self.path)
        if self._inline:
            raw = deep_merge(raw, self._inline)
        if self.overlay is not None:
            raw = deep_merge(raw, _read_yaml(self.overlay))
        return raw

    def _current_mtimes(self) -> tuple[float, float]:
        def mt(p: Path | None) -> float:
            try:
                return p.stat().st_mtime if p is not None else 0.0
            except FileNotFoundError:
                return 0.0
        return mt(self.path), mt(self.overlay)

    def _load(self) -> AppConfig:
        self._mtimes = self._current_mtimes()
        return AppConfig.from_dict(self._raw())

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def version(self) -> str:
        return self._version

    def params_for(self, symbol: str, kind: str) -> Params:
        return self._config.params_for(symbol, kind)

    def on_change(self, cb: Callable[[AppConfig], None]) -> None:
        self._listeners.append(cb)

    def _swap(self, cfg: AppConfig) -> None:
        old = self._version
        self._config = cfg
        self._version = cfg.version()
        if self._version != old:
            log.info("config reloaded: version %s -> %s", old, self._version)
            for cb in list(self._listeners):
                try:
                    cb(cfg)
                except Exception:  # pragma: no cover - listener bugs must not stop reloads
                    log.exception("config listener failed")

    def reload_if_changed(self) -> bool:
        if self._current_mtimes() == self._mtimes:
            return False
        try:
            cfg = self._load()
        except Exception as exc:
            log.error("config reload rejected, keeping version %s: %s", self._version, exc)
            return False
        self._swap(cfg)
        return True

    async def watch(self, interval: float = 2.0) -> None:
        while True:
            await asyncio.sleep(interval)
            self.reload_if_changed()

    # -- edits from the dashboard -------------------------------------------
    def write_overlay(self, patch: dict[str, Any]) -> AppConfig:
        """Deep-merge ``patch`` into the overlay file, validate, and reload.

        Raises ``ValueError`` (pydantic ValidationError) without writing if the
        result would be invalid.
        """
        current_overlay = _read_yaml(self.overlay) if self.overlay is not None else {}
        new_overlay = deep_merge(current_overlay, patch)
        base: dict[str, Any] = _read_yaml(self.path) if self.path is not None else {}
        if self._inline:
            base = deep_merge(base, self._inline)
        cfg = AppConfig.from_dict(deep_merge(base, new_overlay))  # validate first
        if self.overlay is None:
            self._inline = deep_merge(self._inline or {}, patch)
        else:
            self.overlay.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.overlay.with_suffix(".yaml.tmp")
            tmp.write_text(yaml.safe_dump(new_overlay, sort_keys=False), encoding="utf-8")
            tmp.replace(self.overlay)
        self._mtimes = self._current_mtimes()
        self._swap(cfg)
        return cfg


def load_config(path: str | Path | None = None, **overrides: Any) -> AppConfig:
    """Convenience for scripts/tests: load YAML (if any) and apply keyword overrides
    to ``params``."""
    raw = _read_yaml(Path(path)) if path else {}
    if overrides:
        raw = deep_merge(raw, {"params": overrides})
    return AppConfig.from_dict(raw)
