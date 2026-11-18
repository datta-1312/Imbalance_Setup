"""Kite Connect adapter: binary parser, websocket reconnect/resubscribe, REST."""

from __future__ import annotations

import asyncio
import hashlib
import json
import struct
from datetime import date, datetime

import httpx
import pytest

from imbalance.adapters.kite import (KiteRest, KiteTicker, pack_frame, pack_full, parse_frame, parse_packet,
                                     split_packets)
from imbalance.config import KiteConfig
from imbalance.models import Instrument, Kind, Tick

NSE_TOKEN = 738561          # segment byte 0x01 (NSE)
CDS_TOKEN = (5000 << 8) | 3  # currency segment


def sample_tick(token=NSE_TOKEN) -> Tick:
    return Tick(token=token, ts=1794976140.0, ltp=162.35, volume=12_345_678, atp=161.98, recv_ts=1794976140.4,
                last_qty=55, oi=9_876_543, bid_px=(162.30, 162.25, 162.20, 162.15, 162.10),
                bid_qty=(100, 200, 300, 400, 500), ask_px=(162.35, 162.40, 162.45, 162.50, 162.55),
                ask_qty=(110, 220, 330, 440, 550), total_buy_qty=800_000, total_sell_qty=600_000,
                day_open=160.0, day_high=163.0, day_low=159.5, prev_close=160.2, last_trade_ts=1794976139.0)


def test_full_packet_layout_roundtrip():
    t = sample_tick()
    pkt = pack_full(t)
    assert len(pkt) == 184
    # independent check of the documented offsets
    assert struct.unpack(">I", pkt[0:4])[0] == NSE_TOKEN
    assert struct.unpack(">I", pkt[4:8])[0] == 16235          # LTP in paise
    assert struct.unpack(">I", pkt[16:20])[0] == 12_345_678   # day volume
    assert struct.unpack(">I", pkt[60:64])[0] == 1794976140   # exchange timestamp
    assert struct.unpack(">IIH", pkt[64:74]) == (100, 16230, 1)  # first bid entry
    p = parse_packet(pkt, recv_ts=1794976140.4)
    assert p.token == NSE_TOKEN and p.ltp == pytest.approx(162.35) and p.atp == pytest.approx(161.98)
    assert p.volume == 12_345_678 and p.oi == 9_876_543 and p.ts == 1794976140.0
    assert p.bid_px == pytest.approx(t.bid_px) and p.ask_qty == t.ask_qty
    assert (p.total_buy_qty, p.total_sell_qty) == (800_000, 600_000)
    assert p.prev_close == pytest.approx(160.2) and p.recv_ts == 1794976140.4


def test_frame_split_heartbeat_and_other_modes():
    full = pack_full(sample_tick())
    ltp = struct.pack(">II", NSE_TOKEN, 16200)
    index_full = struct.pack(">8I", 256265, 2600000, 2610000, 2590000, 2595000, 2598000, 0, 1794976140)
    quote = struct.pack(">11I", NSE_TOKEN, 16235, 5, 16198, 1000, 10, 20, 16000, 16300, 15950, 16020)
    frame = pack_frame([full, ltp, index_full, quote])
    assert [len(x) for x in split_packets(frame)] == [184, 8, 32, 44]
    ticks = parse_frame(frame, 1.0)
    assert [t.ltp for t in ticks] == pytest.approx([162.35, 162.0, 26000.0, 162.35])
    assert ticks[2].ts == 1794976140.0 and ticks[2].day_high == pytest.approx(26100.0)
    assert ticks[3].volume == 1000 and ticks[3].prev_close == pytest.approx(160.2)
    assert parse_frame(b"\x00", 1.0) == []                     # heartbeat


def test_currency_divisor():
    t = sample_tick(CDS_TOKEN)
    t.ltp, t.atp = 83.1234, 83.12
    p = parse_packet(pack_full(t), 0.0)
    assert p.ltp == pytest.approx(83.1234)


async def test_websocket_subscribe_reconnect_resubscribe():
    from websockets.asyncio.server import serve

    received: list[list[dict]] = []
    conn = {"n": 0}

    async def handler(ws):
        conn["n"] += 1
        msgs = []
        received.append(msgs)
        for _ in range(2):
            msgs.append(json.loads(await ws.recv()))
        await ws.send(pack_frame([pack_full(sample_tick())]))
        if conn["n"] == 1:
            await ws.close()            # server drops the connection
            return
        await ws.send(b"\x00")          # heartbeat, then silence -> client watchdog reconnects
        await asyncio.sleep(1)

    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cfg = KiteConfig(heartbeat_timeout_seconds=0.5, reconnect_initial_delay_seconds=0.05)
        kt = KiteTicker("key", "token", cfg, ws_url=f"ws://127.0.0.1:{port}")
        statuses, ticks = [], []
        kt.on_status = lambda s, ts, info: statuses.append(s)
        kt.on_ticks = lambda ts: ticks.extend(ts)
        await kt.subscribe([NSE_TOKEN, 256265])
        task = asyncio.create_task(kt.run())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if conn["n"] >= 3:
                break
        await kt.close()
        await asyncio.wait_for(task, 5)
    assert received[0] == [{"a": "subscribe", "v": [256265, NSE_TOKEN]},
                           {"a": "mode", "v": ["full", [256265, NSE_TOKEN]]}]
    assert received[1] == received[0]                               # resubscribed after reconnect
    assert statuses[:3] == ["connected", "disconnected", "reconnected"]
    assert "disconnected" in statuses[3:]                           # heartbeat watchdog fired
    assert len(ticks) >= 2 and ticks[0].ltp == pytest.approx(162.35)
    assert kt.reconnects >= 2


def test_connection_split_limits():
    cfg = KiteConfig(max_instruments_per_connection=3, max_connections=2)
    kt = KiteTicker("k", "t", cfg)
    kt.tokens = list(range(8))
    assert kt.chunks() == [[0, 1, 2], [3, 4, 5]]                    # capped at 2 connections


async def test_rest_session_instruments_quotes_historical(tmp_path):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/session/token":
            body = dict(x.split("=") for x in request.content.decode().split("&"))
            want = hashlib.sha256(b"key" + b"req123" + b"secret").hexdigest()
            assert body["checksum"] == want
            return httpx.Response(200, json={"status": "success", "data": {"access_token": "AT", "user_id": "U1"}})
        if request.url.path == "/instruments":
            assert request.headers["Authorization"] == "token key:AT"
            csv = ("instrument_token,exchange_token,tradingsymbol,name,last_price,expiry,strike,tick_size,lot_size,"
                   "instrument_type,segment,exchange\n1,1,ABC,ABC LTD,10,,0,0.05,1,EQ,NSE,NSE\n")
            return httpx.Response(200, text=csv, headers={"content-type": "text/csv"})
        if request.url.path == "/quote":
            keys = request.url.params.get_list("i")
            return httpx.Response(200, json={"status": "success", "data": {
                k: {"lower_circuit_limit": 90.0, "upper_circuit_limit": 110.0} for k in keys}})
        if request.url.path.startswith("/instruments/historical/"):
            return httpx.Response(200, json={"status": "success", "data": {"candles": [
                ["2026-11-17T09:15:00+0530", 100, 101, 99.5, 100.5, 12000, 50000]]}})
        return httpx.Response(404, json={"status": "error", "message": "nope"})

    cfg = KiteConfig(session_file=str(tmp_path / "s.json"), historical_requests_per_second=100)
    rest = KiteRest(cfg, api_key="key", api_secret="secret", transport=httpx.MockTransport(handler))
    assert "api_key=key" in rest.login_url()
    data = await rest.generate_session("req123")
    rest.save_session(data, today=date(2026, 11, 18))
    assert rest.load_session(today=date(2026, 11, 18)) == "AT"
    assert rest.load_session(today=date(2026, 11, 19)) is None       # tokens expire daily
    rest.access_token = "AT"
    rows = await rest.instruments()
    assert rows[0]["tradingsymbol"] == "ABC"
    inst = Instrument(token=1, symbol="ABC", underlying="ABC", kind=Kind.EQ, exchange="NSE")
    assert await rest.price_bands([inst]) == {1: (90.0, 110.0)}
    hist = await rest.historical(1, datetime(2026, 11, 17), datetime(2026, 11, 18))
    assert hist[0]["oi"] == 50000 and hist[0]["ts"] == datetime.fromisoformat("2026-11-17T09:15:00+05:30").timestamp()
    await rest.aclose()
