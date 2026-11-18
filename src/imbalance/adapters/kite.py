"""Zerodha Kite Connect market data (websocket full mode) and REST helpers.

Binary format (as implemented by the official ``kiteconnect`` client):
a frame starts with a uint16 packet count, then each packet is a uint16 length
followed by the packet; all integers are big-endian. Packet sizes: 8 (LTP),
28/32 (index quote/full), 44 (quote), 184 (full). Full packet: 16 uint32
fields (token, LTP, last qty, ATP, day volume, total buy qty, total sell qty,
open, high, low, close, last trade time, OI, OI day high, OI day low,
exchange timestamp) then 10 depth entries of (uint32 qty, uint32 price,
uint16 orders, 2 bytes padding): 5 bids then 5 asks. Prices are in paise
(divide by 100; currency segments use other divisors). A 1-byte message is a
heartbeat. Limits: 3,000 instruments per connection, 3 connections per key.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import struct
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from ..config import KiteConfig
from ..instruments import parse_instrument_master
from ..models import Tick
from ..timeutil import IST
from .base import MarketDataAdapter

log = logging.getLogger(__name__)

SEG_CDS, SEG_BCD, SEG_NCO, SEG_INDICES = 3, 6, 12, 9

_U16 = struct.Struct(">H")
_LTP = struct.Struct(">II")
_INDEX_QUOTE = struct.Struct(">7I")
_INDEX_FULL = struct.Struct(">8I")
_QUOTE = struct.Struct(">11I")
_FULL_HEAD = struct.Struct(">16I")
_DEPTH = struct.Struct(">IIH2x")


def _divisor(token: int) -> float:
    seg = token & 0xFF
    if seg == SEG_CDS:
        return 1e7
    if seg in (SEG_BCD, SEG_NCO):
        return 1e4
    return 100.0


def split_packets(frame: bytes) -> list[bytes]:
    if len(frame) < 2:
        return []   # heartbeat
    n = _U16.unpack_from(frame, 0)[0]
    out = []
    j = 2
    for _ in range(n):
        if j + 2 > len(frame):
            break
        ln = _U16.unpack_from(frame, j)[0]
        out.append(frame[j + 2:j + 2 + ln])
        j += 2 + ln
    return out


def parse_packet(p: bytes, recv_ts: float) -> Tick | None:
    ln = len(p)
    if ln == 184:
        f = _FULL_HEAD.unpack_from(p, 0)
        token = f[0]
        d = _divisor(token)
        bid_px, bid_qty, ask_px, ask_qty = [], [], [], []
        for i in range(10):
            qty, price, _orders = _DEPTH.unpack_from(p, 64 + 12 * i)
            if i < 5:
                bid_qty.append(qty)
                bid_px.append(price / d)
            else:
                ask_qty.append(qty)
                ask_px.append(price / d)
        return Tick(token=token, ts=float(f[15]), ltp=f[1] / d, volume=f[4], atp=f[3] / d, recv_ts=recv_ts,
                    last_qty=f[2], oi=f[12], bid_px=tuple(bid_px), bid_qty=tuple(bid_qty), ask_px=tuple(ask_px),
                    ask_qty=tuple(ask_qty), total_buy_qty=f[5], total_sell_qty=f[6], day_open=f[7] / d,
                    day_high=f[8] / d, day_low=f[9] / d, prev_close=f[10] / d, last_trade_ts=float(f[11]))
    if ln == 44:
        f = _QUOTE.unpack_from(p, 0)
        d = _divisor(f[0])
        return Tick(token=f[0], ts=recv_ts, ltp=f[1] / d, volume=f[4], atp=f[3] / d, recv_ts=recv_ts,
                    last_qty=f[2], total_buy_qty=f[5], total_sell_qty=f[6], day_open=f[7] / d, day_high=f[8] / d,
                    day_low=f[9] / d, prev_close=f[10] / d)
    if ln in (28, 32):
        f = (_INDEX_FULL if ln == 32 else _INDEX_QUOTE).unpack_from(p, 0)
        d = _divisor(f[0])
        ts = float(f[7]) if ln == 32 else recv_ts
        return Tick(token=f[0], ts=ts, ltp=f[1] / d, volume=0, atp=0.0, recv_ts=recv_ts, day_high=f[2] / d,
                    day_low=f[3] / d, day_open=f[4] / d, prev_close=f[5] / d)
    if ln == 8:
        token, ltp = _LTP.unpack_from(p, 0)
        return Tick(token=token, ts=recv_ts, ltp=ltp / _divisor(token), volume=0, atp=0.0, recv_ts=recv_ts)
    return None


def parse_frame(frame: bytes, recv_ts: float) -> list[Tick]:
    out = []
    for p in split_packets(frame):
        t = parse_packet(p, recv_ts)
        if t is not None:
            out.append(t)
    return out


# --------------------------------------------------------------------------- packing (tests, recorder tools)
def pack_full(t: Tick) -> bytes:
    d = _divisor(t.token)
    r = lambda x: int(round(x * d))
    head = _FULL_HEAD.pack(t.token, r(t.ltp), t.last_qty, r(t.atp), t.volume, t.total_buy_qty, t.total_sell_qty,
                           r(t.day_open), r(t.day_high), r(t.day_low), r(t.prev_close), int(t.last_trade_ts),
                           int(t.oi or 0), int(t.oi or 0), int(t.oi or 0), int(t.ts))
    depth = b""
    bids = list(zip(t.bid_qty, t.bid_px)) + [(0, 0.0)] * (5 - len(t.bid_px))
    asks = list(zip(t.ask_qty, t.ask_px)) + [(0, 0.0)] * (5 - len(t.ask_px))
    for qty, px in bids[:5] + asks[:5]:
        depth += _DEPTH.pack(int(qty), r(px), 1)
    return head + depth


def pack_frame(packets: list[bytes]) -> bytes:
    out = _U16.pack(len(packets))
    for p in packets:
        out += _U16.pack(len(p)) + p
    return out


# --------------------------------------------------------------------------- websocket client
class KiteTicker(MarketDataAdapter):
    """Kite Connect websocket in full mode with heartbeat watchdog and automatic
    reconnect + resubscribe. Splits large universes across connections."""

    name = "kite"

    def __init__(self, api_key: str, access_token: str, cfg: KiteConfig, ws_url: str | None = None,
                 clock=time.time):
        super().__init__()
        self.api_key = api_key
        self.access_token = access_token
        self.cfg = cfg
        self.url = f"{ws_url or cfg.ws_url}?api_key={api_key}&access_token={access_token}"
        self.clock = clock
        self._stop = asyncio.Event()
        self._conns: list[asyncio.Task] = []
        self._ws: dict[int, Any] = {}
        self.text_messages: list[dict] = []

    def chunks(self) -> list[list[int]]:
        n = self.cfg.max_instruments_per_connection
        groups = [self.tokens[i:i + n] for i in range(0, len(self.tokens), n)]
        if len(groups) > self.cfg.max_connections:
            log.error("universe of %d tokens exceeds %d connections x %d instruments; extra tokens dropped",
                      len(self.tokens), self.cfg.max_connections, n)
            groups = groups[: self.cfg.max_connections]
        return groups or [[]]

    async def subscribe(self, tokens: Iterable[int]) -> None:
        await super().subscribe(tokens)
        for idx, ws in list(self._ws.items()):
            group = self.chunks()[idx] if idx < len(self.chunks()) else []
            await self._send_subscribe(ws, group)

    async def _send_subscribe(self, ws: Any, tokens: list[int]) -> None:
        if not tokens:
            return
        for i in range(0, len(tokens), 500):
            part = tokens[i:i + 500]
            await ws.send(json.dumps({"a": "subscribe", "v": part}))
            await ws.send(json.dumps({"a": "mode", "v": ["full", part]}))

    async def run(self) -> None:
        self._conns = [asyncio.create_task(self._connection(i, g)) for i, g in enumerate(self.chunks())]
        try:
            await self._stop.wait()
        finally:
            for t in self._conns:
                t.cancel()
            await asyncio.gather(*self._conns, return_exceptions=True)

    async def close(self) -> None:
        self._stop.set()

    async def _connection(self, idx: int, tokens: list[int]) -> None:
        from websockets.asyncio.client import connect

        delay = self.cfg.reconnect_initial_delay_seconds
        first = True
        while not self._stop.is_set():
            try:
                async with connect(self.url, max_size=None, ping_interval=None, open_timeout=30) as ws:
                    self._ws[idx] = ws
                    self.connected = True
                    await self._send_subscribe(ws, self.chunks()[idx] if idx < len(self.chunks()) else tokens)
                    self.emit_status("connected" if first else "reconnected", self.clock(), connection=idx)
                    first = False
                    delay = self.cfg.reconnect_initial_delay_seconds
                    while not self._stop.is_set():
                        msg = await asyncio.wait_for(ws.recv(), timeout=self.cfg.heartbeat_timeout_seconds)
                        now = self.clock()
                        self.last_message_ts = now
                        if isinstance(msg, (bytes, bytearray)):
                            if len(msg) > 1:
                                self.emit_ticks(parse_frame(bytes(msg), now))
                        else:
                            self._on_text(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # connection refused, closed, heartbeat timeout, ...
                if self._stop.is_set():
                    break
                was = self.connected
                self.connected = False
                self._ws.pop(idx, None)
                if was or first:
                    self.emit_status("disconnected", self.clock(), connection=idx, error=type(exc).__name__)
                self.reconnects += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.cfg.reconnect_max_delay_seconds)
        self._ws.pop(idx, None)

    def _on_text(self, msg: str) -> None:
        try:
            data = json.loads(msg)
        except ValueError:
            return
        self.text_messages = (self.text_messages + [data])[-50:]
        if data.get("type") == "error":
            log.error("kite websocket error: %s", data.get("data"))
            self.emit_status("error", self.clock(), message=str(data.get("data")))


# --------------------------------------------------------------------------- REST
class KiteRest:
    """Minimal Kite Connect REST client: login session, instrument dump, quotes
    (price bands), historical candles. Rate-limited for the historical API."""

    def __init__(self, cfg: KiteConfig, api_key: str | None = None, api_secret: str | None = None,
                 access_token: str | None = None, transport=None):
        import httpx

        self.cfg = cfg
        self.api_key = api_key or os.environ.get(cfg.api_key_env, "")
        self.api_secret = api_secret or os.environ.get(cfg.api_secret_env, "")
        self.access_token = access_token
        self._client = httpx.AsyncClient(base_url=cfg.api_root, timeout=30.0, transport=transport)
        self._last_hist = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- session -------------------------------------------------------------
    def login_url(self) -> str:
        return f"{self.cfg.login_url}?api_key={self.api_key}&v=3"

    def _headers(self) -> dict[str, str]:
        h = {"X-Kite-Version": "3"}
        if self.api_key and self.access_token:
            h["Authorization"] = f"token {self.api_key}:{self.access_token}"
        return h

    async def generate_session(self, request_token: str) -> dict:
        checksum = hashlib.sha256((self.api_key + request_token + self.api_secret).encode()).hexdigest()
        r = await self._client.post("/session/token", data={"api_key": self.api_key, "request_token": request_token,
                                                            "checksum": checksum}, headers=self._headers())
        data = self._json(r)
        self.access_token = data["access_token"]
        return data

    def save_session(self, data: dict, today: date | None = None) -> Path:
        p = Path(self.cfg.session_file)
        p.parent.mkdir(parents=True, exist_ok=True)
        today = today or datetime.now(IST).date()
        p.write_text(json.dumps({"date": today.isoformat(), "access_token": data["access_token"],
                                 "user_id": data.get("user_id")}), encoding="utf-8")
        os.chmod(p, 0o600)
        return p

    def load_session(self, today: date | None = None) -> str | None:
        """Access tokens expire daily: a token saved on another day is not used."""
        p = Path(self.cfg.session_file)
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            return None
        today = today or datetime.now(IST).date()
        if d.get("date") != today.isoformat():
            return None
        self.access_token = d.get("access_token")
        return self.access_token

    @staticmethod
    def _json(r) -> Any:
        data = r.json()
        if r.status_code >= 400 or data.get("status") == "error":
            raise RuntimeError(f"kite error {r.status_code}: {data.get('error_type')}: {data.get('message')}")
        return data["data"]

    # -- data -----------------------------------------------------------------
    async def instruments_csv(self) -> str:
        r = await self._client.get("/instruments", headers=self._headers())
        r.raise_for_status()
        return r.text

    async def instruments(self) -> list[dict]:
        return parse_instrument_master(await self.instruments_csv())

    async def quote(self, keys: list[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for i in range(0, len(keys), self.cfg.quote_batch_size):
            part = keys[i:i + self.cfg.quote_batch_size]
            r = await self._client.get("/quote", params=[("i", k) for k in part], headers=self._headers())
            out.update(self._json(r))
        return out

    async def price_bands(self, instruments: Iterable[Any]) -> dict[int, tuple[float, float]]:
        keys = {f"{i.exchange}:{i.symbol}": i.token for i in instruments}
        quotes = await self.quote(sorted(keys))
        bands = {}
        for k, q in quotes.items():
            lo, hi = q.get("lower_circuit_limit"), q.get("upper_circuit_limit")
            if k in keys and lo and hi:
                bands[keys[k]] = (float(lo), float(hi))
        return bands

    async def historical(self, token: int, start: datetime, end: datetime, interval: str = "minute",
                         oi: bool = True) -> list[dict]:
        """1-minute candles, fetched in chunks of 60 days at <= 3 requests/s."""
        out: list[dict] = []
        step = timedelta(days=60)
        cur = start
        while cur < end:
            nxt = min(end, cur + step)
            wait = 1.0 / self.cfg.historical_requests_per_second - (time.monotonic() - self._last_hist)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_hist = time.monotonic()
            r = await self._client.get(
                f"/instruments/historical/{token}/{interval}",
                params={"from": cur.strftime("%Y-%m-%d %H:%M:%S"), "to": nxt.strftime("%Y-%m-%d %H:%M:%S"),
                        "continuous": 0, "oi": 1 if oi else 0},
                headers=self._headers())
            for row in self._json(r)["candles"]:
                rec = {"ts": datetime.fromisoformat(row[0]).timestamp(), "open": row[1], "high": row[2],
                       "low": row[3], "close": row[4], "volume": row[5]}
                if len(row) > 6:
                    rec["oi"] = row[6]
                out.append(rec)
            cur = nxt
        return out
