"""重排序模块。

先提供一个本地可运行的轻量重排器，确保链路真实可测。
同时支持硅基流动和 OpenAI 兼容格式的云端重排服务。
"""
from __future__ import annotations

import logging
from typing import Iterable

import jieba
import requests

from backend.retrieval_config import load_retrieval_config


logger = logging.getLogger(__name__)


def _tokenize(text: str) -> list[str]:
    return [token.strip() for token in jieba.lcut(text or "") if token.strip()]


class LocalOverlapReranker:
    """本地词项重叠重排器。

    它不追求最终效果上限，但能稳定工作，而且能明显改善“召回到了但排序不准”。
    """

    def score(self, query: str, document: str) -> float:
        query_tokens = set(_tokenize(query))
        doc_tokens = set(_tokenize(document))
        if not query_tokens or not doc_tokens:
            return 0.0
        overlap = len(query_tokens & doc_tokens) / max(1, len(query_tokens))
        phrase_bonus = 0.2 if (query and query in (document or "")) else 0.0
        return min(1.0, overlap + phrase_bonus)


class OpenAICompatibleReranker:
    """OpenAI 兼容格式的 rerank 调用器。

    这里按通用 JSON 协议预留，后续接供应商时只需要适配字段。
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not self.base_url or not self.api_key or not self.model:
            raise ValueError("Rerank 云端配置不完整")
        response = requests.post(
            f"{self.base_url}/rerank",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "query": query,
                "documents": documents,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("results") or data.get("data") or []
        scores: list[float] = []
        for item in items:
            if isinstance(item, dict):
                scores.append(float(item.get("score") or item.get("relevance_score") or 0.0))
        if len(scores) != len(documents):
            raise ValueError("Rerank 返回数量与候选文档数量不一致")
        return scores


class SiliconCloudReranker:
    """硅基流动 Rerank 调用器。"""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        if not self.base_url or not self.api_key or not self.model:
            raise ValueError("硅基流动 Rerank 配置不完整")
        response = requests.post(
            f"{self.base_url}/rerank",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "query": query,
                "documents": documents,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("results") or data.get("data") or []
        score_pairs: list[tuple[int, float]] = []
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            doc_index = int(item.get("index", index))
            score = float(item.get("relevance_score") or item.get("score") or 0.0)
            score_pairs.append((doc_index, score))
        scores = [0.0] * len(documents)
        for doc_index, score in score_pairs:
            if 0 <= doc_index < len(scores):
                scores[doc_index] = score
        return scores


class RerankerService:
    """统一重排入口。"""

    def __init__(self):
        self._load()

    def _load(self) -> None:
        config = load_retrieval_config().get("rerank") or {}
        self.enabled = bool(config.get("enabled", True))
        self.provider = str(config.get("provider") or "local_overlap").strip().lower()
        self.model = str(config.get("model") or "local_overlap_v1").strip()
        self.top_n = max(1, int(config.get("top_n") or 5))
        self.candidate_limit = max(self.top_n, int(config.get("candidate_limit") or 12))
        self._backend = None
        if self.provider in ("siliconcloud", "silicon_flow", "siliconflow"):
            base_url = str(config.get("base_url") or "").strip()
            api_key = str(config.get("api_key") or "").strip()
            if base_url and api_key and self.model:
                self._backend = SiliconCloudReranker(
                    base_url=base_url,
                    api_key=api_key,
                    model=self.model,
                )
            else:
                self._backend = LocalOverlapReranker()
        elif self.provider in ("openai_compatible", "openai-compatible"):
            self._backend = OpenAICompatibleReranker(
                base_url=str(config.get("base_url") or "").strip(),
                api_key=str(config.get("api_key") or "").strip(),
                model=self.model,
            )
        else:
            self._backend = LocalOverlapReranker()

    def refresh(self) -> None:
        self._load()

    def rerank(self, query: str, candidates: list[dict], top_k: int) -> list[dict]:
        if not candidates:
            return []
        self._load()
        limited = candidates[: max(top_k, self.candidate_limit)]
        if not self.enabled:
            return limited[:top_k]

        docs = [str(item.get("content") or "") for item in limited]
        try:
            if hasattr(self._backend, "rerank"):
                scores = self._backend.rerank(query, docs)
            else:
                scores = [self._backend.score(query, doc) for doc in docs]
        except Exception as exc:
            logger.warning("Rerank unavailable, fallback to original retrieval order: %s", exc)
            fallback: list[dict] = []
            for item in limited:
                merged = dict(item)
                merged["rerank_score"] = None
                merged["final_score"] = float(item.get("weighted_score") or item.get("score") or 0.0)
                fallback.append(merged)
            fallback.sort(key=lambda item: item.get("final_score", 0.0), reverse=True)
            return fallback[:top_k]

        reranked: list[dict] = []
        for item, rerank_score in zip(limited, scores):
            merged = dict(item)
            merged["rerank_score"] = float(rerank_score)
            # 召回分与重排分混合，避免只靠一边。
            merged["final_score"] = (
                float(item.get("weighted_score") or item.get("score") or 0.0) * 0.45
                + float(rerank_score) * 0.55
            )
            reranked.append(merged)
        reranked.sort(key=lambda item: item.get("final_score", 0.0), reverse=True)
        return reranked[:top_k]

    def health(self) -> dict:
        self._load()
        return {
            "enabled": self.enabled,
            "provider": self.provider,
            "model": self.model,
            "top_n": self.top_n,
            "candidate_limit": self.candidate_limit,
        }


reranker_service = RerankerService()


def rerank_candidates(query: str, candidates: list[dict], top_k: int, config_data: dict | None = None) -> list[dict]:
    """按指定配置执行重排。

    未传配置时继续复用全局服务；传了配置时走一次性的轻量实例，
    这样租户可以拥有自己的 Rerank 策略。
    """
    if config_data is None:
        return reranker_service.rerank(query=query, candidates=candidates, top_k=top_k)
    service = RerankerService()
    service._load = lambda: None  # type: ignore[method-assign]
    rerank_cfg = (config_data.get("rerank") or {}) if isinstance(config_data, dict) else {}
    service.enabled = bool(rerank_cfg.get("enabled", True))
    service.provider = str(rerank_cfg.get("provider") or "local_overlap").strip().lower()
    service.model = str(rerank_cfg.get("model") or "local_overlap_v1").strip()
    service.top_n = max(1, int(rerank_cfg.get("top_n") or 5))
    service.candidate_limit = max(service.top_n, int(rerank_cfg.get("candidate_limit") or 12))
    if service.provider in ("siliconcloud", "silicon_flow", "siliconflow"):
        base_url = str(rerank_cfg.get("base_url") or "").strip()
        api_key = str(rerank_cfg.get("api_key") or "").strip()
        if base_url and api_key and service.model:
            service._backend = SiliconCloudReranker(base_url=base_url, api_key=api_key, model=service.model)
        else:
            service._backend = LocalOverlapReranker()
    elif service.provider in ("openai_compatible", "openai-compatible"):
        service._backend = OpenAICompatibleReranker(
            base_url=str(rerank_cfg.get("base_url") or "").strip(),
            api_key=str(rerank_cfg.get("api_key") or "").strip(),
            model=service.model,
        )
    else:
        service._backend = LocalOverlapReranker()
    return service.rerank(query=query, candidates=candidates, top_k=top_k)
