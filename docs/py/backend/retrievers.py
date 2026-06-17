"""检索后端实现。"""
from __future__ import annotations

import math
import re

import jieba
import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

from backend.app_config import get_knowledge_tiers, resolve_knowledge_tiers
from backend.config import RAG_TOP_K
from backend.embeddings import get_embedding_backend
from backend.knowledge_assets import retrieval_scope_meta_matches
from backend.retrieval_config import (
    get_dense_store_config,
    load_retrieval_config,
    resolve_dense_provider,
    resolve_qdrant_local_path,
)

try:
    from qdrant_client import QdrantClient
    from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams

    QDRANT_CLIENT_AVAILABLE = True
except Exception:  # pragma: no cover - 允许未安装时回退
    QdrantClient = None
    Distance = None
    FieldCondition = None
    Filter = None
    MatchValue = None
    PointStruct = None
    VectorParams = None
    QDRANT_CLIENT_AVAILABLE = False

try:
    from pymilvus import MilvusClient

    MILVUS_CLIENT_AVAILABLE = True
except Exception:  # pragma: no cover
    MilvusClient = None
    MILVUS_CLIENT_AVAILABLE = False


def tokenize_text(text: str) -> list[str]:
    """统一中文分词，保证稀疏检索和本地检索行为一致。"""
    words = jieba.lcut(text)
    return [w.strip() for w in words if w.strip()]


def classify_query_profile(query: str) -> str:
    """根据问题类型粗分检索画像，便于动态调整稀疏 / 稠密权重。"""
    raw = str(query or "").strip()
    text = raw.lower()
    if not text:
        return "faq_semantic"
    if re.search(r"\b[A-Za-z]{2,8}\b", raw) and re.search(
        r"(系统|接口|平台|报文|字段|参数|目录|路径|SAP|ERP|EDI|RFC|ASN|API|SQL)",
        raw,
        flags=re.IGNORECASE,
    ):
        return "identifier_lookup"
    if re.search(r"\b[a-z]{2,}[-_:/][a-z0-9._/-]+\b", text) or re.search(r"\b[a-z]{2,}\d{2,}\b", text):
        return "identifier_lookup"
    if re.search(r"\d{2,}", text) or any(token in text for token in ["编号", "单号", "合同号", "工号", "发票号", "订单号", "接口", "参数", "字段", "目录", "路径"]):
        return "identifier_lookup"
    if any(token in text for token in ["制度", "流程", "步骤", "审批", "规范", "要求", "怎么走", "如何", "怎么办", "报销", "入职", "请假", "采购", "开票"]):
        return "process_policy"
    if any(token in text for token in ["是什么", "什么意思", "作用", "介绍", "总结", "说明", "区别", "为什么"]):
        return "faq_semantic"
    return "keyword_exact"


def _normalize_scores(items: list[dict]) -> dict[tuple[str, str, str], float]:
    scores = [float(item.get("score") or 0.0) for item in items]
    if not scores:
        return {}
    max_score = max(scores)
    min_score = min(scores)
    result: dict[tuple[str, str, str], float] = {}
    for item in items:
        key = (
            str(item.get("source") or ""),
            str(item.get("content") or ""),
            str(item.get("tier") or ""),
        )
        score = float(item.get("score") or 0.0)
        if max_score <= 0:
            normalized = 0.0
        elif max_score == min_score:
            normalized = score / max_score
        else:
            normalized = (score - min_score) / max(max_score - min_score, 1e-9)
        result[key] = float(max(normalized, 0.0))
    return result


class LocalTfidfRetriever:
    """本地 TF-IDF 检索后端。"""

    def __init__(self):
        self._vectorizer = TfidfVectorizer(
            analyzer="word",
            tokenizer=tokenize_text,
            max_features=10000,
            ngram_range=(1, 2),
        )
        self._vectors = None
        self._ready = False

    def build(self, chunks: list[str]) -> None:
        if not chunks:
            self._vectors = None
            self._ready = False
            return
        self._vectors = self._vectorizer.fit_transform(chunks)
        self._ready = True

    def search(
        self,
        query: str,
        chunks: list[str],
        chunk_sources: list[str],
        chunk_tiers: list[str],
        chunk_source_weights: list[float],
        top_k: int = RAG_TOP_K,
        tier_config: dict | None = None,
    ) -> list[dict]:
        if not self._ready or not chunks:
            return []
        try:
            q_vec = self._vectorizer.transform([query])
            raw_sims = cosine_similarity(q_vec, self._vectors)[0]
            weighted_sims = np.array([
                raw_sims[i]
                * chunk_source_weights[i]
                for i in range(len(raw_sims))
            ])
            top_indices = np.argsort(weighted_sims)[::-1][:top_k]
            results = []
            for idx in top_indices:
                if raw_sims[idx] <= 0.01:
                    continue
                tier = chunk_tiers[idx]
                results.append(
                    {
                        "content": chunks[idx],
                        "source": chunk_sources[idx],
                        "score": float(raw_sims[idx]),
                        "weighted_score": float(weighted_sims[idx]),
                        "tier": tier,
                        "tier_label": tier,
                        "backend": "local_tfidf",
                    }
                )
            return results
        except Exception:
            return []

    def health(self) -> dict:
        return {"backend": "local_tfidf", "ready": self._ready}


class BM25Retriever:
    """本地 BM25 稀疏检索后端。"""

    def __init__(self, config_data: dict | None = None):
        self._config_data = config_data or {}
        self._docs_tokens: list[list[str]] = []
        self._doc_freqs: dict[str, int] = {}
        self._doc_lengths: list[int] = []
        self._avg_doc_len = 0.0
        self._ready = False

    def _cfg(self) -> dict:
        sparse_cfg = (self._config_data or {}).get("sparse") or {}
        return {
            "enabled": bool(sparse_cfg.get("enabled", True)),
            "k1": float(sparse_cfg.get("k1", 1.5)),
            "b": float(sparse_cfg.get("b", 0.75)),
        }

    def build(self, chunks: list[str]) -> None:
        if not chunks:
            self._docs_tokens = []
            self._doc_freqs = {}
            self._doc_lengths = []
            self._avg_doc_len = 0.0
            self._ready = False
            return
        self._docs_tokens = [tokenize_text(chunk) for chunk in chunks]
        self._doc_lengths = [len(tokens) for tokens in self._docs_tokens]
        total_len = sum(self._doc_lengths)
        self._avg_doc_len = total_len / len(self._doc_lengths) if self._doc_lengths else 0.0
        doc_freqs: dict[str, int] = {}
        for tokens in self._docs_tokens:
            for token in set(tokens):
                doc_freqs[token] = doc_freqs.get(token, 0) + 1
        self._doc_freqs = doc_freqs
        self._ready = True

    def _idf(self, term: str) -> float:
        total_docs = len(self._docs_tokens)
        doc_freq = self._doc_freqs.get(term, 0)
        if total_docs == 0:
            return 0.0
        return math.log(((total_docs - doc_freq + 0.5) / (doc_freq + 0.5)) + 1.0)

    def search(
        self,
        query: str,
        chunks: list[str],
        chunk_sources: list[str],
        chunk_tiers: list[str],
        chunk_source_weights: list[float],
        top_k: int = RAG_TOP_K,
        tier_config: dict | None = None,
    ) -> list[dict]:
        if not self._ready or not chunks:
            return []
        cfg = self._cfg()
        if not cfg["enabled"]:
            return []
        query_terms = tokenize_text(query)
        if not query_terms:
            return []
        k1 = cfg["k1"]
        b = cfg["b"]
        raw_scores: list[float] = []
        for idx, tokens in enumerate(self._docs_tokens):
            if not tokens:
                raw_scores.append(0.0)
                continue
            doc_len = self._doc_lengths[idx] or 1
            term_freqs: dict[str, int] = {}
            for token in tokens:
                term_freqs[token] = term_freqs.get(token, 0) + 1
            score = 0.0
            for term in query_terms:
                freq = term_freqs.get(term, 0)
                if freq <= 0:
                    continue
                idf = self._idf(term)
                numerator = freq * (k1 + 1.0)
                denominator = freq + k1 * (1.0 - b + b * doc_len / max(self._avg_doc_len, 1.0))
                score += idf * (numerator / max(denominator, 1e-9))
            raw_scores.append(score)
        max_score = max(raw_scores) if raw_scores else 0.0
        if max_score <= 0:
            return []
        weighted_scores = []
        for idx, raw_score in enumerate(raw_scores):
            normalized = raw_score / max_score
            tier = chunk_tiers[idx]
            weighted_scores.append(normalized * float(chunk_source_weights[idx]))
        top_indices = np.argsort(np.array(weighted_scores))[::-1][:top_k]
        results: list[dict] = []
        for idx in top_indices:
            raw_score = raw_scores[idx]
            if raw_score <= 0:
                continue
            tier = chunk_tiers[idx]
            normalized = raw_score / max_score
            results.append(
                {
                    "content": chunks[idx],
                    "source": chunk_sources[idx],
                    "score": float(normalized),
                    "weighted_score": float(weighted_scores[idx]),
                    "tier": tier,
                    "tier_label": tier,
                    "backend": "bm25",
                }
            )
        return results

    def health(self) -> dict:
        cfg = self._cfg()
        return {
            "backend": "bm25",
            "enabled": bool(cfg["enabled"]),
            "ready": self._ready,
            "docs": len(self._docs_tokens),
            "avg_doc_len": round(self._avg_doc_len, 2),
        }


class QdrantRetriever:
    """Qdrant 检索后端。

    当前先用本地哈希向量做可运行版本，先把向量库链路跑通，
    后面再平滑替换成真实 Embedding 服务。
    """

    def __init__(
        self,
        config_data: dict | None = None,
        knowledge_tiers: dict | None = None,
        tenant_namespace: str | None = None,
    ):
        self._client = None
        self._last_error = ""
        self._config_data = config_data
        self._knowledge_tiers = knowledge_tiers
        self._tenant_namespace = str(tenant_namespace or "").strip()
        self._connect()

    def _cfg(self) -> dict:
        return self._config_data or load_retrieval_config()

    def _tiers(self) -> dict:
        return self._knowledge_tiers or get_knowledge_tiers()

    def _connect(self) -> None:
        cfg = get_dense_store_config(self._cfg(), provider="qdrant")
        if not cfg.get("enabled"):
            self._last_error = "qdrant_disabled"
            return
        if not QDRANT_CLIENT_AVAILABLE or QdrantClient is None:
            self._last_error = "qdrant_client_missing"
            return
        mode = str(cfg.get("mode") or "local").strip().lower()
        try:
            if mode == "local":
                store_path = resolve_qdrant_local_path(cfg.get("path"))
                store_path.parent.mkdir(parents=True, exist_ok=True)
                self._client = QdrantClient(path=str(store_path))
            else:
                self._client = QdrantClient(
                    url=str(cfg.get("url") or ""),
                    api_key=str(cfg.get("api_key") or "") or None,
                    timeout=3.0,
                )
            self._client.get_collections()
            self._last_error = ""
        except Exception as exc:
            self._client = None
            self._last_error = str(exc)

    def is_ready(self) -> bool:
        return self._client is not None

    def _embedding_size(self) -> int:
        cfg = get_dense_store_config(self._cfg(), provider="qdrant")
        return int(cfg.get("vector_size") or 256)

    def ensure_collection(self) -> bool:
        if not self.is_ready():
            return False
        cfg = get_dense_store_config(self._cfg(), provider="qdrant")
        collection = str(cfg.get("collection") or "").strip()
        if not collection:
            self._last_error = "missing_collection"
            return False
        distance_name = str(cfg.get("distance") or "Cosine").strip().lower()
        distance_map = {
            "cosine": Distance.COSINE if Distance else None,
            "dot": Distance.DOT if Distance else None,
            "euclid": Distance.EUCLID if Distance else None,
        }
        distance = distance_map.get(distance_name) or (Distance.COSINE if Distance else None)
        if VectorParams is None or distance is None:
            self._last_error = "qdrant_models_missing"
            return False
        try:
            existing = self._client.collection_exists(collection_name=collection)
            if not existing:
                self._client.create_collection(
                    collection_name=collection,
                    vectors_config=VectorParams(size=self._embedding_size(), distance=distance),
                )
            self._last_error = ""
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    def sync_documents(
        self,
        chunks: list[str],
        chunk_sources: list[str],
        chunk_tiers: list[str],
        chunk_source_weights: list[float],
        chunk_scope_meta: list[dict] | None = None,
    ) -> dict:
        if not self.ensure_collection():
            return {"ok": False, "msg": self._last_error or "Qdrant 未就绪", "indexed": 0}
        if not chunks:
            self._last_error = ""
            return {"ok": True, "msg": "当前没有知识片段，已跳过同步", "indexed": 0}
        cfg = get_dense_store_config(self._cfg(), provider="qdrant")
        collection = str(cfg.get("collection") or "").strip()
        if PointStruct is None:
            return {"ok": False, "msg": "qdrant models unavailable", "indexed": 0}
        embedder = get_embedding_backend(self._cfg())
        vectors = embedder.embed_many(chunks)
        points: list[PointStruct] = []
        for idx, content in enumerate(chunks):
            scope_meta = dict((chunk_scope_meta or [])[idx] or {}) if chunk_scope_meta and idx < len(chunk_scope_meta) else {}
            points.append(
                PointStruct(
                    id=idx + 1,
                    vector=vectors[idx],
                    payload={
                        "content": content,
                        "source": chunk_sources[idx],
                        "tier": chunk_tiers[idx],
                        "source_weight": float(chunk_source_weights[idx]),
                        "tenant_namespace": self._tenant_namespace,
                        "library_id": str(scope_meta.get("library_id") or ""),
                        "category_id": str(scope_meta.get("category_id") or ""),
                        "tags": list(scope_meta.get("tags") or []),
                        "file_key": str(scope_meta.get("file_key") or ""),
                        "tier_code": str(scope_meta.get("tier_code") or ""),
                    },
                )
            )
        try:
            self._client.upsert(collection_name=collection, points=points, wait=True)
            self._last_error = ""
            return {"ok": True, "msg": "Qdrant 索引已同步", "indexed": len(points)}
        except Exception as exc:
            self._last_error = str(exc)
            return {"ok": False, "msg": str(exc), "indexed": 0}

    def search(self, query: str, top_k: int = RAG_TOP_K, knowledge_scope: dict | None = None) -> list[dict]:
        if not self.ensure_collection():
            return []
        cfg = get_dense_store_config(self._cfg(), provider="qdrant")
        collection = str(cfg.get("collection") or "").strip()
        try:
            embedder = get_embedding_backend(self._cfg())
            query_filter = None
            if self._tenant_namespace and Filter and FieldCondition and MatchValue:
                query_filter = Filter(
                    must=[
                        FieldCondition(
                            key="tenant_namespace",
                            match=MatchValue(value=self._tenant_namespace),
                        )
                    ]
                )
            hits = self._client.search(
                collection_name=collection,
                query_vector=embedder.embed(query),
                limit=top_k,
                with_payload=True,
                query_filter=query_filter,
            )
        except Exception as exc:
            self._last_error = str(exc)
            return []
        results: list[dict] = []
        for hit in hits:
            payload = hit.payload or {}
            tier = str(payload.get("tier") or "permanent")
            source_weight = float(payload.get("source_weight") or 1.0)
            score = float(hit.score or 0.0)
            weighted_score = score * source_weight
            results.append(
                {
                    "content": str(payload.get("content") or ""),
                    "source": str(payload.get("source") or ""),
                    "score": score,
                    "weighted_score": weighted_score,
                    "tier": tier,
                    "tier_label": tier,
                    "backend": "dense",
                    "dense_provider": "qdrant",
                    "library_id": str(payload.get("library_id") or ""),
                    "category_id": str(payload.get("category_id") or ""),
                    "tags": list(payload.get("tags") or []),
                    "file_key": str(payload.get("file_key") or ""),
                    "tier_code": str(payload.get("tier_code") or ""),
                }
            )
        filtered = [
            item for item in results
            if retrieval_scope_meta_matches(
                {
                    **item,
                    "source": str(item.get("source") or ""),
                    "tier": str(item.get("tier") or ""),
                },
                knowledge_scope,
            )
        ]
        return sorted(filtered, key=lambda item: item["weighted_score"], reverse=True)

    def health(self) -> dict:
        cfg = get_dense_store_config(self._cfg(), provider="qdrant")
        path = ""
        if str(cfg.get("mode") or "local").strip().lower() == "local":
            path = str(resolve_qdrant_local_path(cfg.get("path")))
        return {
            "backend": "dense",
            "provider": "qdrant",
            "enabled": bool(cfg.get("enabled")),
            "mode": str(cfg.get("mode") or "local"),
            "client_available": QDRANT_CLIENT_AVAILABLE,
            "ready": self.is_ready(),
            "collection": str(cfg.get("collection") or ""),
            "url": str(cfg.get("url") or ""),
            "path": path,
            "last_error": self._last_error,
        }


class MilvusRetriever:
    """Milvus 检索后端。"""

    def __init__(
        self,
        config_data: dict | None = None,
        knowledge_tiers: dict | None = None,
        tenant_namespace: str | None = None,
    ):
        self._client = None
        self._last_error = ""
        self._config_data = config_data
        self._knowledge_tiers = knowledge_tiers
        self._tenant_namespace = str(tenant_namespace or "").strip()
        self._connect()

    def _cfg(self) -> dict:
        return self._config_data or load_retrieval_config()

    def _dense_cfg(self) -> dict:
        return get_dense_store_config(self._cfg(), provider="milvus")

    def _metric_type(self) -> str:
        cfg = self._dense_cfg()
        return str(cfg.get("metric_type") or "COSINE").strip().upper() or "COSINE"

    def _tenant_filter_expr(self) -> str:
        if not self._tenant_namespace:
            return ""
        escaped = self._tenant_namespace.replace("\\", "\\\\").replace('"', '\\"')
        return f'tenant_namespace == "{escaped}"'

    def _normalize_search_score(self, raw_value: float) -> float:
        metric_type = self._metric_type()
        if metric_type in {"L2", "EUCLID"}:
            return 1.0 / (1.0 + max(raw_value, 0.0))
        return raw_value

    def _connect(self) -> None:
        cfg = self._dense_cfg()
        if not cfg.get("enabled"):
            self._last_error = "milvus_disabled"
            return
        if not MILVUS_CLIENT_AVAILABLE or MilvusClient is None:
            self._last_error = "milvus_client_missing"
            return
        uri = str(cfg.get("uri") or cfg.get("url") or "").strip()
        if not uri:
            self._last_error = "milvus_uri_missing"
            return
        kwargs = {"uri": uri}
        token = str(cfg.get("token") or "").strip()
        if token:
            kwargs["token"] = token
        else:
            user = str(cfg.get("user") or "").strip()
            password = str(cfg.get("password") or "").strip()
            if user or password:
                kwargs["user"] = user
                kwargs["password"] = password
        db_name = str(cfg.get("db_name") or "").strip()
        if db_name:
            kwargs["db_name"] = db_name
        try:
            self._client = MilvusClient(**kwargs)
            self._client.list_collections()
            self._last_error = ""
        except Exception as exc:
            self._client = None
            self._last_error = str(exc)

    def is_ready(self) -> bool:
        return self._client is not None

    def _embedding_size(self) -> int:
        cfg = self._dense_cfg()
        return int(cfg.get("vector_size") or 256)

    def ensure_collection(self) -> bool:
        if not self.is_ready():
            return False
        cfg = self._dense_cfg()
        collection = str(cfg.get("collection") or "").strip()
        if not collection:
            self._last_error = "missing_collection"
            return False
        metric_type = self._metric_type()
        try:
            exists = bool(self._client.has_collection(collection_name=collection))
            if not exists:
                self._client.create_collection(
                    collection_name=collection,
                    dimension=self._embedding_size(),
                    metric_type=metric_type,
                    auto_id=False,
                    enable_dynamic_field=True,
                )
            try:
                self._client.load_collection(collection_name=collection)
            except Exception:
                pass
            self._last_error = ""
            return True
        except Exception as exc:
            self._last_error = str(exc)
            return False

    def sync_documents(
        self,
        chunks: list[str],
        chunk_sources: list[str],
        chunk_tiers: list[str],
        chunk_source_weights: list[float],
        chunk_scope_meta: list[dict] | None = None,
    ) -> dict:
        if not self.ensure_collection():
            return {"ok": False, "msg": self._last_error or "Milvus 未就绪", "indexed": 0}
        if not chunks:
            self._last_error = ""
            return {"ok": True, "msg": "当前没有知识片段，已跳过同步", "indexed": 0}
        cfg = self._dense_cfg()
        collection = str(cfg.get("collection") or "").strip()
        embedder = get_embedding_backend(self._cfg())
        vectors = embedder.embed_many(chunks)
        rows: list[dict] = []
        for idx, content in enumerate(chunks):
            scope_meta = dict((chunk_scope_meta or [])[idx] or {}) if chunk_scope_meta and idx < len(chunk_scope_meta) else {}
            rows.append(
                {
                    "id": idx + 1,
                    "vector": vectors[idx],
                    "content": content,
                    "source": chunk_sources[idx],
                    "tier": chunk_tiers[idx],
                    "source_weight": float(chunk_source_weights[idx]),
                    "tenant_namespace": self._tenant_namespace,
                    "library_id": str(scope_meta.get("library_id") or ""),
                    "category_id": str(scope_meta.get("category_id") or ""),
                    "tags": list(scope_meta.get("tags") or []),
                    "file_key": str(scope_meta.get("file_key") or ""),
                    "tier_code": str(scope_meta.get("tier_code") or ""),
                }
            )
        try:
            try:
                self._client.delete(collection_name=collection, filter="id >= 0")
            except Exception:
                pass
            self._client.insert(collection_name=collection, data=rows)
            try:
                self._client.load_collection(collection_name=collection)
            except Exception:
                pass
            self._last_error = ""
            return {"ok": True, "msg": "Milvus 索引已同步", "indexed": len(rows)}
        except Exception as exc:
            self._last_error = str(exc)
            return {"ok": False, "msg": str(exc), "indexed": 0}

    def search(self, query: str, top_k: int = RAG_TOP_K, knowledge_scope: dict | None = None) -> list[dict]:
        if not self.ensure_collection():
            return []
        cfg = self._dense_cfg()
        collection = str(cfg.get("collection") or "").strip()
        try:
            embedder = get_embedding_backend(self._cfg())
            query_filter = self._tenant_filter_expr()
            raw_hits = self._client.search(
                collection_name=collection,
                data=[embedder.embed(query)],
                filter=query_filter,
                limit=top_k,
                output_fields=[
                    "content",
                    "source",
                    "tier",
                    "source_weight",
                    "tenant_namespace",
                    "library_id",
                    "category_id",
                    "tags",
                    "file_key",
                    "tier_code",
                ],
            )
        except Exception as exc:
            self._last_error = str(exc)
            return []
        hits = raw_hits[0] if raw_hits and isinstance(raw_hits[0], list) else raw_hits
        results: list[dict] = []
        for hit in hits or []:
            payload = hit.get("entity") if isinstance(hit, dict) else {}
            payload = payload if isinstance(payload, dict) else {}
            if self._tenant_namespace and str(payload.get("tenant_namespace") or "").strip() not in {"", self._tenant_namespace}:
                continue
            tier = str(payload.get("tier") or "permanent")
            source_weight = float(payload.get("source_weight") or 1.0)
            raw_score = float(hit.get("distance") or hit.get("score") or 0.0) if isinstance(hit, dict) else 0.0
            score = self._normalize_search_score(raw_score)
            weighted_score = score * source_weight
            results.append(
                {
                    "content": str(payload.get("content") or ""),
                    "source": str(payload.get("source") or ""),
                    "score": score,
                    "raw_score": raw_score,
                    "weighted_score": weighted_score,
                    "tier": tier,
                    "tier_label": tier,
                    "backend": "dense",
                    "dense_provider": "milvus",
                    "library_id": str(payload.get("library_id") or ""),
                    "category_id": str(payload.get("category_id") or ""),
                    "tags": list(payload.get("tags") or []),
                    "file_key": str(payload.get("file_key") or ""),
                    "tier_code": str(payload.get("tier_code") or ""),
                }
            )
        filtered = [
            item for item in results
            if retrieval_scope_meta_matches(
                {
                    **item,
                    "source": str(item.get("source") or ""),
                    "tier": str(item.get("tier") or ""),
                },
                knowledge_scope,
            )
        ]
        return sorted(filtered, key=lambda item: item["weighted_score"], reverse=True)

    def health(self) -> dict:
        cfg = self._dense_cfg()
        return {
            "backend": "dense",
            "provider": "milvus",
            "enabled": bool(cfg.get("enabled")),
            "client_available": MILVUS_CLIENT_AVAILABLE,
            "ready": self.is_ready(),
            "collection": str(cfg.get("collection") or ""),
            "uri": str(cfg.get("uri") or cfg.get("url") or ""),
            "db_name": str(cfg.get("db_name") or ""),
            "last_error": self._last_error,
        }


def build_dense_retriever(
    *,
    config_data: dict | None = None,
    knowledge_tiers: dict | None = None,
    tenant_namespace: str | None = None,
):
    provider = resolve_dense_provider(config_data)
    if provider == "milvus":
        return MilvusRetriever(
            config_data=config_data,
            knowledge_tiers=knowledge_tiers,
            tenant_namespace=tenant_namespace,
        )
    return QdrantRetriever(
        config_data=config_data,
        knowledge_tiers=knowledge_tiers,
        tenant_namespace=tenant_namespace,
    )


class HybridRetriever:
    """混合检索后端：融合稠密检索与稀疏检索结果。"""

    def __init__(
        self,
        *,
        dense_retriever,
        sparse_retriever: BM25Retriever,
        config_data: dict | None = None,
    ):
        self._dense = dense_retriever
        self._sparse = sparse_retriever
        self._config_data = config_data or {}

    def _cfg(self) -> dict:
        sparse_cfg = (self._config_data or {}).get("sparse") or {}
        return {
            "enabled": bool(sparse_cfg.get("enabled", True)),
            "dense_weight": float(sparse_cfg.get("dense_weight", 0.6)),
            "sparse_weight": float(sparse_cfg.get("sparse_weight", 0.4)),
            "fusion_alpha": float(sparse_cfg.get("fusion_alpha", 0.7)),
            "rrf_k": int(sparse_cfg.get("rrf_k", 50)),
            "query_profiles": dict(sparse_cfg.get("query_profiles") or {}),
        }

    def _resolve_profile(self, query: str) -> dict:
        cfg = self._cfg()
        profile_name = classify_query_profile(query)
        profile_cfg = dict((cfg.get("query_profiles") or {}).get(profile_name) or {})
        dense_weight = float(profile_cfg.get("dense_weight", cfg["dense_weight"]))
        sparse_weight = float(profile_cfg.get("sparse_weight", cfg["sparse_weight"]))
        fusion_alpha = float(profile_cfg.get("fusion_alpha", cfg["fusion_alpha"]))
        total = dense_weight + sparse_weight
        if total <= 0:
            dense_weight, sparse_weight = cfg["dense_weight"], cfg["sparse_weight"]
            total = dense_weight + sparse_weight
        dense_weight /= total
        sparse_weight /= total
        fusion_alpha = min(max(fusion_alpha, 0.0), 1.0)
        return {
            "name": profile_name,
            "dense_weight": dense_weight,
            "sparse_weight": sparse_weight,
            "fusion_alpha": fusion_alpha,
            "rrf_k": int(cfg["rrf_k"]),
        }

    def search(
        self,
        *,
        query: str,
        dense_results: list[dict],
        sparse_results: list[dict],
        top_k: int,
    ) -> list[dict]:
        cfg = self._cfg()
        if not cfg["enabled"]:
            return dense_results[:top_k]
        profile = self._resolve_profile(query)
        dense_weight = profile["dense_weight"]
        sparse_weight = profile["sparse_weight"]
        fusion_alpha = profile["fusion_alpha"]
        rrf_k = int(profile["rrf_k"])
        merged: dict[tuple[str, str, str], dict] = {}
        dense_norm = _normalize_scores(dense_results)
        sparse_norm = _normalize_scores(sparse_results)
        dense_rank = {
            (
                str(item.get("source") or ""),
                str(item.get("content") or ""),
                str(item.get("tier") or ""),
            ): index + 1
            for index, item in enumerate(dense_results)
        }
        sparse_rank = {
            (
                str(item.get("source") or ""),
                str(item.get("content") or ""),
                str(item.get("tier") or ""),
            ): index + 1
            for index, item in enumerate(sparse_results)
        }

        def merge_one(item: dict, source_kind: str, source_weight: float) -> None:
            key = (
                str(item.get("source") or ""),
                str(item.get("content") or ""),
                str(item.get("tier") or ""),
            )
            existing = merged.get(key)
            normalized = dense_norm.get(key, 0.0) if source_kind == "dense" else sparse_norm.get(key, 0.0)
            rank_map = dense_rank if source_kind == "dense" else sparse_rank
            rank = rank_map.get(key, 999999)
            rrf_score = 1.0 / (rrf_k + rank)
            weighted = (fusion_alpha * normalized + (1.0 - fusion_alpha) * rrf_score) * source_weight
            if existing is None:
                merged[key] = {
                    **item,
                    "score": float(item.get("score") or 0.0),
                    "weighted_score": weighted,
                    "backend": "hybrid",
                    "hybrid_sources": [source_kind],
                    "query_profile": profile["name"],
                    "dense_score": float(dense_norm.get(key, 0.0)),
                    "sparse_score": float(sparse_norm.get(key, 0.0)),
                    "fusion_alpha": float(fusion_alpha),
                    "dense_weight": float(dense_weight),
                    "sparse_weight": float(sparse_weight),
                }
                return
            existing["weighted_score"] = float(existing.get("weighted_score") or 0.0) + weighted
            existing_sources = set(existing.get("hybrid_sources") or [])
            existing_sources.add(source_kind)
            existing["hybrid_sources"] = sorted(existing_sources)
            existing["query_profile"] = profile["name"]
            existing["dense_score"] = float(dense_norm.get(key, 0.0))
            existing["sparse_score"] = float(sparse_norm.get(key, 0.0))
            existing["fusion_alpha"] = float(fusion_alpha)
            existing["dense_weight"] = float(dense_weight)
            existing["sparse_weight"] = float(sparse_weight)

        for item in dense_results:
            merge_one(item, "dense", dense_weight)
        for item in sparse_results:
            merge_one(item, "sparse", sparse_weight)

        results = sorted(
            merged.values(),
            key=lambda value: float(value.get("weighted_score") or 0.0),
            reverse=True,
        )
        return results[:top_k]

    def health(self) -> dict:
        cfg = self._cfg()
        return {
            "backend": "hybrid",
            "enabled": bool(cfg["enabled"]),
            "dense_weight": cfg["dense_weight"],
            "sparse_weight": cfg["sparse_weight"],
            "fusion_alpha": cfg["fusion_alpha"],
            "rrf_k": cfg["rrf_k"],
            "profiles": sorted((cfg.get("query_profiles") or {}).keys()),
        }
