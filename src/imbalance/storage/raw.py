"""Raw snapshot recording as compressed daily Parquet (spec §8.8), plus feed
disconnect/reconnect events and every clock advance of the live timer, in
processing order, so a replay reproduces the live run exactly.

Layout: ``<data_dir>/raw/<YYYY-MM-DD>/part-<NNNNN>.parquet``. Each flush appends a
row group to the open part; parts rotate every ``rotate_minutes`` so a crash
loses at most the open part.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date
from pathlib import Path
from typing import Iterator

import pyarrow as pa
import pyarrow.parquet as pq

from ..models import Tick
from ..scenario import FeedEvent, Record

log = logging.getLogger(__name__)

KIND_TICK, KIND_DISCONNECT, KIND_RECONNECT, KIND_CLOCK = 0, 1, 2, 3
_FEED_KINDS = {"disconnect": KIND_DISCONNECT, "reconnect": KIND_RECONNECT, "clock": KIND_CLOCK}
_FEED_NAMES = {v: k for k, v in _FEED_KINDS.items()}

SCHEMA = pa.schema([
    ("kind", pa.int8()), ("token", pa.int64()), ("ts", pa.float64()), ("recv_ts", pa.float64()),
    ("ltp", pa.float64()), ("last_qty", pa.int64()), ("atp", pa.float64()), ("volume", pa.int64()),
    ("oi", pa.int64()), ("tbq", pa.int64()), ("tsq", pa.int64()), ("day_open", pa.float64()),
    ("day_high", pa.float64()), ("day_low", pa.float64()), ("prev_close", pa.float64()), ("ltt", pa.float64()),
    ("bid_px", pa.list_(pa.float64())), ("bid_qty", pa.list_(pa.int64())),
    ("ask_px", pa.list_(pa.float64())), ("ask_qty", pa.list_(pa.int64())),
])
_COLS = [f.name for f in SCHEMA]


class RawTickRecorder:
    def __init__(self, data_dir: str | Path, day: date, rotate_minutes: int = 10, compression: str = "zstd"):
        self.dir = Path(data_dir) / "raw" / day.isoformat()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.rotate_s = rotate_minutes * 60.0
        self.compression = compression
        self._buf: dict[str, list] = {c: [] for c in _COLS}
        self._writer: pq.ParquetWriter | None = None
        self._part_started: float | None = None
        self._part = len(list(self.dir.glob("part-*.parquet")))
        self._lock = threading.Lock()
        self.rows = 0

    def __len__(self) -> int:
        return len(self._buf["kind"])

    def add(self, t: Tick) -> None:
        b = self._buf
        b["kind"].append(KIND_TICK)
        b["token"].append(t.token)
        b["ts"].append(t.ts)
        b["recv_ts"].append(t.recv_ts)
        b["ltp"].append(t.ltp)
        b["last_qty"].append(t.last_qty)
        b["atp"].append(t.atp)
        b["volume"].append(t.volume)
        b["oi"].append(t.oi)
        b["tbq"].append(t.total_buy_qty)
        b["tsq"].append(t.total_sell_qty)
        b["day_open"].append(t.day_open)
        b["day_high"].append(t.day_high)
        b["day_low"].append(t.day_low)
        b["prev_close"].append(t.prev_close)
        b["ltt"].append(t.last_trade_ts)
        b["bid_px"].append(list(t.bid_px))
        b["bid_qty"].append(list(t.bid_qty))
        b["ask_px"].append(list(t.ask_px))
        b["ask_qty"].append(list(t.ask_qty))

    def add_feed_event(self, kind: str, ts: float, recv_ts: float | None = None) -> None:
        b = self._buf
        for c in _COLS:
            b[c].append(None)
        b["kind"][-1] = _FEED_KINDS[kind]
        b["token"][-1] = 0
        b["ts"][-1] = ts
        b["recv_ts"][-1] = recv_ts if recv_ts is not None else ts

    def add_clock(self, ts: float) -> None:
        self.add_feed_event("clock", ts, ts)

    def _swap(self) -> dict[str, list] | None:
        if not self._buf["kind"]:
            return None
        cols, self._buf = self._buf, {c: [] for c in _COLS}
        return cols

    def _write(self, cols: dict[str, list]) -> None:
        with self._lock:
            table = pa.Table.from_pydict(cols, schema=SCHEMA)
            first = cols["recv_ts"][0] or 0.0
            if self._writer is not None and self._part_started is not None and first - self._part_started >= self.rotate_s:
                self._close_writer()
            if self._writer is None:
                path = self.dir / f"part-{self._part:05d}.parquet"
                self._part += 1
                self._writer = pq.ParquetWriter(path, SCHEMA, compression=self.compression)
                self._part_started = first
            self._writer.write_table(table)
            self.rows += table.num_rows

    def _close_writer(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self._part_started = None

    def flush(self) -> None:
        cols = self._swap()
        if cols:
            self._write(cols)

    async def flush_async(self) -> None:
        cols = self._swap()
        if cols:
            await asyncio.to_thread(self._write, cols)

    async def flush_loop(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.flush_async()
            except Exception:  # disk full etc.: keep running, the live screen matters more
                log.exception("raw tick flush failed")

    def close(self) -> None:
        self.flush()
        with self._lock:
            self._close_writer()


def raw_dir(data_dir: str | Path, day: date) -> Path:
    return Path(data_dir) / "raw" / day.isoformat()


def read_raw(directory: str | Path) -> list[Record]:
    """All records of a recorded day in processing (file) order."""
    out: list[Record] = []
    for path in sorted(Path(directory).glob("part-*.parquet")):
        try:
            pf = pq.ParquetFile(path)
        except Exception:
            log.warning("skipping unreadable part %s (incomplete footer?)", path)
            continue
        for batch in pf.iter_batches():
            out.extend(_records(batch.to_pydict()))
    return out


def iter_raw(directory: str | Path) -> Iterator[Record]:
    yield from read_raw(directory)


def _records(d: dict[str, list]) -> list[Record]:
    out: list[Record] = []
    n = len(d["kind"])
    for i in range(n):
        k = d["kind"][i]
        if k != KIND_TICK:
            out.append(FeedEvent(_FEED_NAMES.get(k, "disconnect"), d["ts"][i], d["recv_ts"][i]))
            continue
        out.append(Tick(token=d["token"][i], ts=d["ts"][i], ltp=d["ltp"][i], volume=d["volume"][i], atp=d["atp"][i],
                        recv_ts=d["recv_ts"][i], last_qty=d["last_qty"][i] or 0, oi=d["oi"][i],
                        bid_px=tuple(d["bid_px"][i] or ()), bid_qty=tuple(d["bid_qty"][i] or ()),
                        ask_px=tuple(d["ask_px"][i] or ()), ask_qty=tuple(d["ask_qty"][i] or ()),
                        total_buy_qty=d["tbq"][i] or 0, total_sell_qty=d["tsq"][i] or 0,
                        day_open=d["day_open"][i] or 0.0, day_high=d["day_high"][i] or 0.0,
                        day_low=d["day_low"][i] or 0.0, prev_close=d["prev_close"][i] or 0.0,
                        last_trade_ts=d["ltt"][i] or 0.0))
    return out
