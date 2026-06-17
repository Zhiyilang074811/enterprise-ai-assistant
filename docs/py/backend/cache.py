"""Redis 优先的问答缓存。

当前产品要求缓存用于“同租户、同问题”的高并发复用，
而不是把“看起来有点像”的问题也强行命中同一答案。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any

from backend.config import CACHE_TTL_SECONDS

try:
    from redis import Redis
except Exception:  # pragma: no cover - 允许未安装 redis 时回退
    Redis = None


class SemanticCache:
    """支持 Redis 持久化的精确问答缓存。"""

    def __init__(self):
        self._entries: list[tuple[str, dict[str, Any], float]] = []
        self._redis = self._build_redis_client()
        self._redis_loaded_at = 0.0

    def _build_redis_client(self):
        redis_url = os.environ.get("REDIS_URL", "").strip()
        if not redis_url or Redis is None:
            return None
        try:
            client = Redis.from_url(redis_url, decode_responses=True)
            client.ping()
            return client
        except Exception:
            return None

    def _cache_key(self, question: str) -> str:
        digest = hashlib.sha256(question.strip().encode("utf-8")).hexdigest()
        return f"rag:semantic:exact:{digest}"

    def _entries_key(self) -> str:
        return "rag:semantic:entries"

    def _normalize_payload(self, question: str, payload: Any) -> dict[str, Any] | None:
        clean_q = question.strip()
        if not clean_q:
            return None
        if isinstance(payload, dict):
            answer = str(payload.get("answer", "") or "").strip()
            if not answer:
                return None
            return {
                "question": clean_q,
                "answer": answer,
                "knowledge_hits": list(payload.get("knowledge_hits") or []),
                "retrieval_trace": dict(payload.get("retrieval_trace") or {}),
            }
        answer = str(payload or "").strip()
        if not answer:
            return None
        return {
            "question": clean_q,
            "answer": answer,
            "knowledge_hits": [],
            "retrieval_trace": {},
        }

    def _decode_cached_value(self, question: str, raw_value: Any) -> dict[str, Any] | None:
        if raw_value in (None, ""):
            return None
        if isinstance(raw_value, dict):
            return self._normalize_payload(question, raw_value)
        text = str(raw_value)
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = text
        return self._normalize_payload(question, parsed)

    def _rebuild_index(self):
        return

    def _evict_expired_local(self):
        now = time.time()
        before = len(self._entries)
        self._entries = [e for e in self._entries if now - e[2] < CACHE_TTL_SECONDS]
        if len(self._entries) != before:
            self._rebuild_index()

    def _load_entries_from_redis(self, force: bool = False):
        if not self._redis:
            return
        now = time.time()
        if not force and self._entries and now - self._redis_loaded_at < 15:
            return
        raw_entries = self._redis.lrange(self._entries_key(), 0, -1)
        entries: list[tuple[str, dict[str, Any], float]] = []
        for raw in raw_entries:
            try:
                item = json.loads(raw)
                ts = float(item.get("timestamp", 0) or 0)
                if now - ts >= CACHE_TTL_SECONDS:
                    continue
                question = str(item.get("question", ""))
                payload = self._normalize_payload(question, item.get("payload") or item)
                if payload:
                    entries.append((question, payload, ts))
            except Exception:
                continue
        self._entries = entries
        self._redis_loaded_at = now
        self._rebuild_index()

    def _persist_entry(self, question: str, payload: dict[str, Any], timestamp: float):
        if not self._redis:
            return
        payload = json.dumps(
            {"question": question, "payload": payload, "timestamp": timestamp},
            ensure_ascii=False,
        )
        try:
            pipe = self._redis.pipeline()
            pipe.setex(self._cache_key(question), CACHE_TTL_SECONDS, json.dumps(json.loads(payload)["payload"], ensure_ascii=False))
            pipe.rpush(self._entries_key(), payload)
            pipe.expire(self._entries_key(), CACHE_TTL_SECONDS)
            pipe.execute()
        except Exception:
            return

    def _clear_redis(self):
        if not self._redis:
            return
        try:
            self._redis.delete(self._entries_key())
        except Exception:
            return

    def health(self) -> dict[str, Any]:
        """返回缓存健康状态，用于后台可观测性。"""
        return {
            "backend": "redis" if self._redis else "memory",
            "entries": len(self._entries),
            "ttl_seconds": CACHE_TTL_SECONDS,
            "mode": "exact",
        }

    def get(self, question: str) -> dict[str, Any] | None:
        clean = question.strip()
        self._evict_expired_local()
        if self._redis:
            try:
                exact = self._decode_cached_value(clean, self._redis.get(self._cache_key(clean)))
            except Exception:
                exact = None
            if exact:
                return exact
            self._load_entries_from_redis()
        for cached_question, cached_payload, _ in reversed(self._entries):
            if cached_question == clean:
                return dict(cached_payload)
        return None

    def put(
        self,
        question: str,
        answer: str,
        *,
        knowledge_hits: list[dict] | None = None,
        retrieval_trace: dict[str, Any] | None = None,
    ):
        payload = self._normalize_payload(
            question,
            {
                "answer": answer,
                "knowledge_hits": knowledge_hits or [],
                "retrieval_trace": retrieval_trace or {},
            },
        )
        if not payload:
            return
        ts = time.time()
        self._entries.append((payload["question"], payload, ts))
        self._rebuild_index()
        self._persist_entry(payload["question"], payload, ts)

    def clear(self):
        self._entries.clear()
        self._fitted = False
        self._vectors = None
        self._redis_loaded_at = 0.0
        self._clear_redis()


semantic_cache = SemanticCache()
