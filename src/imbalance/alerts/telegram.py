"""Telegram delivery with rate limiting and 429 back-off. The bot token is read
from the environment and never logged."""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from ..config import TelegramConfig

log = logging.getLogger(__name__)


class TelegramSender:
    def __init__(self, token: str, chat_id: str, api_base: str = "https://api.telegram.org",
                 min_interval: float = 1.0, transport: httpx.AsyncBaseTransport | None = None):
        self._token = token
        self.chat_id = chat_id
        self.api_base = api_base.rstrip("/")
        self.min_interval = min_interval
        self._transport = transport
        self._q: asyncio.Queue[str] = asyncio.Queue(maxsize=500)
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self._last = 0.0

    @classmethod
    def from_config(cls, cfg: TelegramConfig) -> "TelegramSender | None":
        if not cfg.enabled:
            return None
        token, chat = os.environ.get(cfg.bot_token_env), os.environ.get(cfg.chat_id_env)
        if not token or not chat:
            log.warning("telegram enabled but %s / %s not set; telegram alerts disabled",
                        cfg.bot_token_env, cfg.chat_id_env)
            return None
        return cls(token, chat, cfg.api_base, cfg.min_interval_seconds)

    def enqueue(self, text: str) -> None:
        try:
            self._q.put_nowait(text[:4000])
        except asyncio.QueueFull:
            self.dropped += 1

    @property
    def pending(self) -> int:
        return self._q.qsize()

    async def run(self) -> None:
        async with httpx.AsyncClient(timeout=10.0, transport=self._transport) as client:
            while True:
                text = await self._q.get()
                wait = self._last + self.min_interval - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
                await self._send(client, text)
                self._last = time.monotonic()

    async def _send(self, client: httpx.AsyncClient, text: str) -> None:
        url = f"{self.api_base}/bot{self._token}/sendMessage"
        for _ in range(3):
            try:
                r = await client.post(url, json={"chat_id": self.chat_id, "text": text,
                                                 "disable_web_page_preview": True})
            except httpx.HTTPError as exc:
                log.warning("telegram send failed: %s", type(exc).__name__)
                await asyncio.sleep(1.0)
                continue
            if r.status_code == 429:
                try:
                    retry = float(r.json().get("parameters", {}).get("retry_after", 1))
                except ValueError:
                    retry = 1.0
                await asyncio.sleep(min(retry, 30.0))
                continue
            if r.status_code >= 400:
                log.warning("telegram rejected message: HTTP %s", r.status_code)
                self.failed += 1
                return
            self.sent += 1
            return
        self.failed += 1
