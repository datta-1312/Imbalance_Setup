"""Candle storage (spec §8.8): PostgreSQL/TimescaleDB in production, SQLite for
a single machine and tests. Stored candles feed the baseline engine once enough
sessions have been recorded (exact turnover beats the historical-API estimate).
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import date
from pathlib import Path
from typing import Iterable, Protocol

from ..baseline import MinuteBar
from ..models import Candle, Flag
from ..timeutil import day_start, minute_of_day

log = logging.getLogger(__name__)

_COLUMNS = ("token", "ts", "symbol", "open", "high", "low", "close", "turnover_cr", "volume", "buy_vol",
            "sell_vol", "unk_vol", "vwap", "oi_open", "oi_close", "n_ticks", "flags")


def _row(c: Candle, symbol: str) -> tuple:
    return (c.token, c.start, symbol, c.open, c.high, c.low, c.close, c.turnover_cr, c.volume, c.buy_vol,
            c.sell_vol, c.unk_vol, c.vwap, c.oi_open, c.oi_close, c.n_ticks, int(c.flags))


def _candle(r) -> Candle:
    return Candle(token=r[0], start=float(r[1]), open=r[3], high=r[4], low=r[5], close=r[6], turnover_cr=r[7],
                  volume=r[8], buy_vol=r[9], sell_vol=r[10], unk_vol=r[11], vwap=r[12], oi_open=r[13],
                  oi_close=r[14], n_ticks=r[15], flags=Flag(r[16]), closed=True)


class CandleStore(Protocol):
    def write(self, candles: Iterable[Candle], symbols: dict[int, str]) -> None: ...
    def read(self, token: int, start: float, end: float) -> list[Candle]: ...
    def close(self) -> None: ...


class SQLiteCandleStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS candles (token INTEGER, ts REAL, symbol TEXT, open REAL, high REAL, low REAL,"
            " close REAL, turnover_cr REAL, volume INTEGER, buy_vol INTEGER, sell_vol INTEGER, unk_vol INTEGER,"
            " vwap REAL, oi_open INTEGER, oi_close INTEGER, n_ticks INTEGER, flags INTEGER,"
            " PRIMARY KEY (token, ts))")
        self._db.commit()

    def write(self, candles: Iterable[Candle], symbols: dict[int, str]) -> None:
        rows = [_row(c, symbols.get(c.token, "")) for c in candles]
        if not rows:
            return
        with self._lock:
            self._db.executemany(f"INSERT OR REPLACE INTO candles ({','.join(_COLUMNS)}) VALUES "
                                 f"({','.join('?' * len(_COLUMNS))})", rows)
            self._db.commit()

    def read(self, token: int, start: float, end: float) -> list[Candle]:
        with self._lock:
            cur = self._db.execute(f"SELECT {','.join(_COLUMNS)} FROM candles WHERE token=? AND ts>=? AND ts<? "
                                   "ORDER BY ts", (token, start, end))
            return [_candle(r) for r in cur.fetchall()]

    def tokens(self) -> list[int]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT DISTINCT token FROM candles ORDER BY token")]

    def close(self) -> None:
        with self._lock:
            self._db.close()


class PostgresCandleStore:
    """PostgreSQL; becomes a TimescaleDB hypertable when the extension exists."""

    def __init__(self, dsn: str):
        import psycopg

        self._lock = threading.Lock()
        self._db = psycopg.connect(dsn, autocommit=True)
        with self._db.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS candles (token BIGINT NOT NULL, ts DOUBLE PRECISION NOT NULL,"
                " time TIMESTAMPTZ NOT NULL, symbol TEXT, open DOUBLE PRECISION, high DOUBLE PRECISION,"
                " low DOUBLE PRECISION, close DOUBLE PRECISION, turnover_cr DOUBLE PRECISION, volume BIGINT,"
                " buy_vol BIGINT, sell_vol BIGINT, unk_vol BIGINT, vwap DOUBLE PRECISION, oi_open BIGINT,"
                " oi_close BIGINT, n_ticks INTEGER, flags INTEGER, PRIMARY KEY (token, time))")
            self.timescale = False
            try:
                cur.execute("SELECT 1 FROM pg_available_extensions WHERE name='timescaledb'")
                if cur.fetchone():
                    cur.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
                    cur.execute("SELECT create_hypertable('candles', 'time', if_not_exists => TRUE, "
                                "migrate_data => TRUE)")
                    self.timescale = True
            except Exception as exc:  # pragma: no cover - depends on the server
                log.warning("timescaledb not enabled: %s", exc)

    def write(self, candles: Iterable[Candle], symbols: dict[int, str]) -> None:
        rows = []
        for c in candles:
            r = _row(c, symbols.get(c.token, ""))
            rows.append((r[0], r[1], c.start, *r[2:]))      # token, ts, time (epoch -> timestamptz), ...
        if not rows:
            return
        cols = ("token", "ts", "time") + _COLUMNS[2:]
        placeholders = ",".join(["%s", "%s", "to_timestamp(%s)"] + ["%s"] * (len(_COLUMNS) - 2))
        update = ",".join(f"{c}=EXCLUDED.{c}" for c in _COLUMNS[2:])
        with self._lock, self._db.cursor() as cur:
            cur.executemany(f"INSERT INTO candles ({','.join(cols)}) VALUES ({placeholders}) "
                            f"ON CONFLICT (token, time) DO UPDATE SET {update}", rows)

    def read(self, token: int, start: float, end: float) -> list[Candle]:
        with self._lock, self._db.cursor() as cur:
            cur.execute(f"SELECT {','.join(_COLUMNS)} FROM candles WHERE token=%s AND ts>=%s AND ts<%s ORDER BY ts",
                        (token, start, end))
            return [_candle(r) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()


def open_store(backend: str, sqlite_path: str | None = None, dsn: str | None = None) -> CandleStore | None:
    if backend == "none":
        return None
    if backend == "postgres":
        if not dsn:
            raise ValueError("postgres candle backend needs a DSN (storage.postgres_dsn_env)")
        return PostgresCandleStore(dsn)
    return SQLiteCandleStore(sqlite_path or "data/candles.sqlite")


class AsyncCandleWriter:
    """Writes each minute's candles from a worker thread so the event loop never blocks."""

    def __init__(self, store: CandleStore, symbols: dict[int, str]):
        self.store = store
        self.symbols = symbols
        self._q: asyncio.Queue[list[Candle]] = asyncio.Queue(maxsize=1000)
        self.written = 0
        self.dropped = 0

    def submit(self, candles: list[Candle]) -> None:
        try:
            self._q.put_nowait(candles)
        except asyncio.QueueFull:
            self.dropped += len(candles)

    async def run(self) -> None:
        while True:
            batch = await self._q.get()
            try:
                await asyncio.to_thread(self.store.write, batch, self.symbols)
                self.written += len(batch)
            except Exception:
                log.exception("candle write failed")


def sessions_from_store(store: CandleStore, token: int, days: list[date]) -> list[list[MinuteBar]]:
    """Per-session minute bars for the baseline engine (oldest first)."""
    out = []
    for d in days:
        start = day_start(d)
        cs = store.read(token, start, start + 86400)
        out.append([MinuteBar(minute_of_day(c.start), c.open, c.high, c.low, c.close, c.volume, c.turnover_cr,
                              c.oi_close) for c in cs if not (c.flags & (Flag.GAP | Flag.PARTIAL))])
    return out
