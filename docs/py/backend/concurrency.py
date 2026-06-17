"""Lightweight in-process concurrency guards for chat and workflow traffic."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from contextlib import asynccontextmanager
from dataclasses import dataclass

from backend.config import (
    AGENT_CHAT_CONCURRENCY_LIMIT,
    GLOBAL_CHAT_CONCURRENCY_LIMIT,
    GLOBAL_LLM_CONCURRENCY_LIMIT,
    GLOBAL_WORKFLOW_IO_CONCURRENCY_LIMIT,
    TENANT_CHAT_CONCURRENCY_LIMIT,
)


@dataclass
class BusyError(RuntimeError):
    scope: str
    key: str
    limit: int
    current: int

    def __str__(self) -> str:
        return f"{self.scope} busy: {self.current}/{self.limit}"


class ScopedLimiter:
    """Simple non-blocking concurrency limiter keyed by scope."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def acquire(self, scope: str, key: str, limit: int):
        if limit <= 0:
            yield
            return
        async with self._lock:
            current = self._counts[key]
            if current >= limit:
                raise BusyError(scope=scope, key=key, limit=limit, current=current)
            self._counts[key] = current + 1
        try:
            yield
        finally:
            async with self._lock:
                remaining = max(0, self._counts.get(key, 1) - 1)
                if remaining:
                    self._counts[key] = remaining
                else:
                    self._counts.pop(key, None)


_chat_global_limiter = ScopedLimiter()
_chat_tenant_limiter = ScopedLimiter()
_chat_agent_limiter = ScopedLimiter()
_llm_limiter = ScopedLimiter()
_workflow_io_limiter = ScopedLimiter()


@asynccontextmanager
async def acquire_chat_slots(*, tenant_id: str, agent_id: str = ""):
    clean_tenant = str(tenant_id or "default").strip() or "default"
    clean_agent = str(agent_id or "").strip()
    async with _chat_global_limiter.acquire("global_chat", "global", GLOBAL_CHAT_CONCURRENCY_LIMIT):
        async with _chat_tenant_limiter.acquire("tenant_chat", clean_tenant, TENANT_CHAT_CONCURRENCY_LIMIT):
            if clean_agent:
                agent_key = f"{clean_tenant}:{clean_agent}"
                async with _chat_agent_limiter.acquire("agent_chat", agent_key, AGENT_CHAT_CONCURRENCY_LIMIT):
                    yield
            else:
                yield


@asynccontextmanager
async def acquire_llm_slot():
    async with _llm_limiter.acquire("llm", "global", GLOBAL_LLM_CONCURRENCY_LIMIT):
        yield


@asynccontextmanager
async def acquire_workflow_io_slot():
    async with _workflow_io_limiter.acquire("workflow_io", "global", GLOBAL_WORKFLOW_IO_CONCURRENCY_LIMIT):
        yield


def get_concurrency_snapshot() -> dict:
    return {
        "limits": {
            "global_chat": GLOBAL_CHAT_CONCURRENCY_LIMIT,
            "tenant_chat": TENANT_CHAT_CONCURRENCY_LIMIT,
            "agent_chat": AGENT_CHAT_CONCURRENCY_LIMIT,
            "llm": GLOBAL_LLM_CONCURRENCY_LIMIT,
            "workflow_io": GLOBAL_WORKFLOW_IO_CONCURRENCY_LIMIT,
        },
        "active": {
            "global_chat": sum(_chat_global_limiter._counts.values()),
            "tenant_chat_keys": len(_chat_tenant_limiter._counts),
            "agent_chat_keys": len(_chat_agent_limiter._counts),
            "llm": sum(_llm_limiter._counts.values()),
            "workflow_io": sum(_workflow_io_limiter._counts.values()),
        },
    }
