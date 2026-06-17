"""Embedding 生成模块。

默认提供本地可运行的哈希向量实现，保证项目在没有云端依赖时也能工作。
同时支持硅基流动和 OpenAI 兼容接口，方便后续切到正式云端检索。
"""
from __future__ import annotations

import hashlib
from typing import Iterable

import jieba
import numpy as np
import requests

from backend.retrieval_config import load_retrieval_config, resolve_dense_vector_size


class LocalHashEmbedding:
    """本地哈希向量。

    这是一个过渡实现，目标是先让向量库链路稳定工作。
    真正上线时可以把 provider 切到云端 embedding。
    """

    def __init__(self, dimensions: int = 1024):
        self.dimensions = max(64, int(dimensions or 1024))

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return [token.strip() for token in jieba.lcut(text or "") if token.strip()]

    def embed(self, text: str) -> list[float]:
        vector = np.zeros(self.dimensions, dtype=np.float32)
        for token in self._tokenize(text):
            digest = hashlib.md5(token.encode("utf-8")).hexdigest()
            index = int(digest[:8], 16) % self.dimensions
            sign = 1.0 if int(digest[8:10], 16) % 2 == 0 else -1.0
            vector[index] += sign
        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector.astype(float).tolist()

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        return [self.embed(text) for text in texts]


class OpenAICompatibleEmbedding:
    """OpenAI 兼容接口的 embedding 调用器。"""

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.max_batch_size = 16
        self.max_batch_chars = 12000

    def _request(self, input_payload: list[str]) -> list[list[float]]:
        if not self.base_url or not self.api_key or not self.model:
            raise ValueError("Embedding 云端配置不完整")
        response = requests.post(
            f"{self.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "input": input_payload,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("data") or []
        vectors = [item.get("embedding") for item in items if isinstance(item, dict)]
        if len(vectors) != len(input_payload):
            raise ValueError("Embedding 返回数量与输入数量不一致")
        return vectors

    def embed(self, text: str) -> list[float]:
        return self._request([text])[0]

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        text_list = list(texts)
        if not text_list:
            return []
        vectors: list[list[float]] = []
        batch: list[str] = []
        batch_chars = 0
        for text in text_list:
            item = str(text or "")
            item_chars = len(item)
            if batch and (len(batch) >= self.max_batch_size or batch_chars + item_chars > self.max_batch_chars):
                vectors.extend(self._request(batch))
                batch = []
                batch_chars = 0
            batch.append(item)
            batch_chars += item_chars
        if batch:
            vectors.extend(self._request(batch))
        if len(vectors) != len(text_list):
            raise ValueError("Embedding 批量返回数量与输入数量不一致")
        return vectors


class SiliconCloudEmbedding(OpenAICompatibleEmbedding):
    """硅基流动 Embedding。

    接口路径就是 `/v1/embeddings`，格式与 OpenAI 兼容。
    """


class QianfanEmbedding:
    """百度智能云千帆 Embedding。

    官方文档使用 `/v2/embeddings`，并通过 Bearer API Key 鉴权。
    """

    def __init__(self, base_url: str, api_key: str, model: str):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or ""
        self.max_batch_size = 16
        self.max_batch_chars = 12000

    def _request(self, input_payload: list[str]) -> list[list[float]]:
        if not self.base_url or not self.api_key or not self.model:
            raise ValueError("千帆 Embedding 配置不完整")
        response = requests.post(
            f"{self.base_url}/embeddings",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "input": input_payload,
            },
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("data") or []
        vectors = [item.get("embedding") for item in items if isinstance(item, dict)]
        if len(vectors) != len(input_payload):
            raise ValueError("千帆 Embedding 返回数量与输入数量不一致")
        return vectors

    def embed(self, text: str) -> list[float]:
        return self._request([text])[0]

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        text_list = list(texts)
        if not text_list:
            return []
        vectors: list[list[float]] = []
        batch: list[str] = []
        batch_chars = 0
        for text in text_list:
            item = str(text or "")
            item_chars = len(item)
            if batch and (len(batch) >= self.max_batch_size or batch_chars + item_chars > self.max_batch_chars):
                vectors.extend(self._request(batch))
                batch = []
                batch_chars = 0
            batch.append(item)
            batch_chars += item_chars
        if batch:
            vectors.extend(self._request(batch))
        if len(vectors) != len(text_list):
            raise ValueError("Embedding 批量返回数量与输入数量不一致")
        return vectors


def get_embedding_backend(config_data: dict | None = None):
    """按当前配置返回 Embedding 后端。"""
    config = config_data or load_retrieval_config()
    embedding_cfg = config.get("embedding") or {}
    dimensions = resolve_dense_vector_size(config)
    provider = str(embedding_cfg.get("provider") or "local_hash").strip().lower()
    base_url = str(embedding_cfg.get("base_url") or "").strip()
    api_key = str(embedding_cfg.get("api_key") or "").strip()
    model = str(embedding_cfg.get("model") or "").strip()
    if provider in ("siliconcloud", "silicon_flow", "siliconflow"):
        if base_url and api_key and model:
            return SiliconCloudEmbedding(base_url=base_url, api_key=api_key, model=model)
        return LocalHashEmbedding(dimensions=dimensions)
    if provider in ("qianfan", "baidu_qianfan", "baidu-qianfan"):
        if base_url and api_key and model:
            return QianfanEmbedding(base_url=base_url, api_key=api_key, model=model)
        return LocalHashEmbedding(dimensions=dimensions)
    if provider in ("openai_compatible", "openai-compatible"):
        if base_url and api_key and model:
            return OpenAICompatibleEmbedding(base_url=base_url, api_key=api_key, model=model)
        return LocalHashEmbedding(dimensions=dimensions)
    return LocalHashEmbedding(dimensions=dimensions)
