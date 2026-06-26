"""Shared, reusable httpx.AsyncClient with idle-timeout recycling.

Previously, four separate modules (ghsync, axiom_logs, bot, notify) each
duplicated the same ~20-line `_get_client()` pattern. This module centralizes
it into a single ReusableClient class with per-module tuning.

Benefits:
  - DRY: one implementation, four configs.
  - Memory: connection pools are closed on idle timeout, not held forever.
  - Testability: easy to mock.
"""
from __future__ import annotations

import asyncio
import time

import httpx


class ReusableClient:
    """A lazily-created, auto-recycling httpx.AsyncClient.

    The client is created on first `get()` and reused for `idle_timeout`
    seconds. After that, it's closed and a fresh one is created on next use.
    This avoids holding keepalive connections indefinitely on idle services.
    """

    __slots__ = ("_timeout", "_limits", "_idle", "_client", "_ts", "_lock")

    def __init__(
        self,
        *,
        timeout: float = 10.0,
        connect: float = 5.0,
        max_connections: int = 4,
        max_keepalive: int = 2,
        idle_timeout: float = 300.0,
    ) -> None:
        self._timeout = httpx.Timeout(timeout, connect=connect)
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive,
        )
        self._idle = idle_timeout
        self._client: httpx.AsyncClient | None = None
        self._ts: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> httpx.AsyncClient:
        async with self._lock:
            now = time.time()
            if (
                self._client
                and not self._client.is_closed
                and (now - self._ts) < self._idle
            ):
                return self._client
            if self._client and not self._client.is_closed:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
            self._client = httpx.AsyncClient(
                timeout=self._timeout, limits=self._limits
            )
            self._ts = now
            return self._client

    async def close(self) -> None:
        async with self._lock:
            if self._client and not self._client.is_closed:
                try:
                    await self._client.aclose()
                except Exception:
                    pass
            self._client = None


# ── pre-configured instances per module ──────────────────────────────
# Each module imports its own instance. Limits are conservative for Railway.
from . import config  # noqa: E402

gh_client = ReusableClient(
    timeout=config.HTTP_TIMEOUT_GH,
    max_connections=2,
    max_keepalive=1,
    idle_timeout=config.HTTP_IDLE_TIMEOUT,
)

axiom_client = ReusableClient(
    timeout=config.HTTP_TIMEOUT_AXIOM,
    max_connections=4,
    max_keepalive=2,
    idle_timeout=config.HTTP_IDLE_TIMEOUT,
)

tg_client = ReusableClient(
    timeout=config.HTTP_TIMEOUT_TG,
    max_connections=4,
    max_keepalive=2,
    idle_timeout=config.HTTP_IDLE_TIMEOUT,
)


async def close_all() -> None:
    """Called during shutdown to release all pooled connections."""
    await gh_client.close()
    await axiom_client.close()
    await tg_client.close()
