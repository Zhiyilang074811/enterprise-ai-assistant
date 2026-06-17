"""RAG 引擎：负责知识装载、切片与检索后端调度。"""
from __future__ import annotations

import glob
import os
import re
import time

from backend.app_config import get_knowledge_tiers, get_runtime_knowledge_dir, resolve_knowledge_tiers
from backend.config import RAG_CHUNK_SIZE
from backend.document_processing import split_documents_for_stats
from backend.knowledge_assets import resolve_retrieval_scope_meta, retrieval_scope_meta_matches
from backend.langchain_components import build_langchain_retrieval_stack
from backend.retrieval_config import load_retrieval_config, normalize_retrieval_backend_name
from backend.retrievers import BM25Retriever, HybridRetriever, LocalTfidfRetriever, build_dense_retriever
from backend.rerankers import rerank_candidates, reranker_service


class RAGEngine:
    """统一的 RAG 调度入口。"""

    def __init__(
        self,
        *,
        knowledge_dir: str | None = None,
        app_config: dict | None = None,
        retrieval_config: dict | None = None,
        knowledge_namespace: str | None = None,
    ):
        self._knowledge_dir = knowledge_dir
        self._app_config = app_config
        self._retrieval_config = retrieval_config
        self._knowledge_namespace = str(
            knowledge_namespace
            or (app_config or {}).get("knowledge_namespace")
            or ""
        ).strip()
        self.chunks: list[str] = []
        self.chunk_sources: list[str] = []
        self.chunk_tiers: list[str] = []
        self.chunk_source_weights: list[float] = []
        self.chunk_scope_meta: list[dict] = []
        self._last_build_time = 0.0
        self._local_retriever = LocalTfidfRetriever()
        self._bm25_retriever = BM25Retriever(config_data=self._retrieval_cfg())
        self._dense_retriever = build_dense_retriever(
            config_data=self._retrieval_cfg(),
            knowledge_tiers=self._tier_cfg(),
            tenant_namespace=self._knowledge_namespace,
        )
        self._hybrid_retriever = HybridRetriever(
            dense_retriever=self._dense_retriever,
            sparse_retriever=self._bm25_retriever,
            config_data=self._retrieval_cfg(),
        )

    def _runtime_knowledge_dir(self) -> str:
        return self._knowledge_dir or get_runtime_knowledge_dir()

    def _tier_cfg(self) -> dict:
        if self._app_config is not None:
            return resolve_knowledge_tiers(self._app_config)
        return get_knowledge_tiers()

    def _retrieval_cfg(self) -> dict:
        return self._retrieval_config or load_retrieval_config()

    def _load_markdown_files(self) -> list[tuple[str, str, str]]:
        """从分层知识库读取 Markdown 文档。"""
        docs: list[tuple[str, str, str]] = []
        knowledge_dir = self._runtime_knowledge_dir()
        knowledge_tiers = self._tier_cfg()
        pattern = os.path.join(knowledge_dir, "**", "*.md")
        for fpath in glob.glob(pattern, recursive=True):
            rel_path = os.path.relpath(fpath, knowledge_dir)
            if rel_path.startswith(".upload_tmp") or f"/.upload_tmp/" in rel_path.replace("\\", "/"):
                continue
            with open(fpath, "r", encoding="utf-8") as f:
                content = f.read()
            first_segment = rel_path.replace("\\", "/").split("/", 1)[0]
            inferred_tier = first_segment if first_segment in knowledge_tiers else "permanent"
            docs.append((rel_path, content, inferred_tier))
        return docs

    @staticmethod
    def _extract_doc_weight(text: str) -> float:
        """从文档元数据推导可信度权重。"""
        confidence_weight = 1.0
        manual_review_weight = 1.0
        confidence_match = re.search(r"来源可信度：([ABC])", text)
        if confidence_match:
            confidence_weight = {"A": 1.15, "B": 1.0, "C": 0.8}.get(confidence_match.group(1), 1.0)
        review_match = re.search(r"是否需人工复核：(是|否)", text)
        if review_match and review_match.group(1) == "是":
            manual_review_weight = 0.85
        return confidence_weight * manual_review_weight

    def _chunk_text(self, text: str, source: str) -> list[tuple[str, str]]:
        """按段落和标题切分文本。"""
        try:
            structured_docs = split_documents_for_stats(text)
        except Exception:
            structured_docs = []
        if structured_docs:
            chunks: list[tuple[str, str]] = []
            for doc in structured_docs:
                content = str(doc.page_content or "").strip()
                if not content:
                    continue
                heading_path = str((doc.metadata or {}).get("heading_path") or "").strip()
                if heading_path and not content.startswith("#"):
                    content = f"## {heading_path}\n{content}"
                chunks.append((content, source))
            if chunks:
                return chunks
        sections = re.split(r"\n(?=#{1,4}\s)", text)
        chunks: list[tuple[str, str]] = []
        for section in sections:
            section = section.strip()
            if not section:
                continue
            if len(section) <= RAG_CHUNK_SIZE:
                chunks.append((section, source))
                continue
            paragraphs = section.split("\n\n")
            current = ""
            for para in paragraphs:
                if len(current) + len(para) > RAG_CHUNK_SIZE:
                    if current:
                        chunks.append((current.strip(), source))
                    current = para
                else:
                    current = current + "\n\n" + para if current else para
            if current.strip():
                chunks.append((current.strip(), source))
        return chunks

    def _select_backend(self, backend_override: str | None = None) -> str:
        """根据配置选择当前检索后端。"""
        if backend_override:
            preferred = normalize_retrieval_backend_name(backend_override)
        else:
            cfg = self._retrieval_cfg()
            preferred = normalize_retrieval_backend_name(cfg.get("backend") or "local_tfidf")
        if preferred == "hybrid":
            if self._dense_retriever.is_ready():
                return "hybrid"
            if self._bm25_retriever.health().get("ready"):
                return "bm25"
        if preferred == "dense" and self._dense_retriever.is_ready():
            return "dense"
        if preferred == "bm25" and self._bm25_retriever.health().get("ready"):
            return "bm25"
        return "local_tfidf"

    @staticmethod
    def _candidate_limit(top_k: int) -> int:
        """候选召回条数略大于最终返回条数，给重排留空间。"""
        rerank_cfg = load_retrieval_config().get("rerank") or {}
        configured = int(rerank_cfg.get("candidate_limit") or top_k)
        return max(top_k, configured)

    def _candidate_limit_for_config(self, top_k: int) -> int:
        rerank_cfg = self._retrieval_cfg().get("rerank") or {}
        configured = int(rerank_cfg.get("candidate_limit") or top_k)
        return max(top_k, configured)

    def build_index(self) -> int:
        """重建当前知识切片索引。"""
        docs = self._load_markdown_files()
        self.chunks = []
        self.chunk_sources = []
        self.chunk_tiers = []
        self.chunk_source_weights = []
        self.chunk_scope_meta = []
        for fname, content, tier in docs:
            doc_weight = self._extract_doc_weight(content)
            scope_meta = resolve_retrieval_scope_meta(
                tenant_id=self._knowledge_namespace or "default",
                source=fname,
                tier=tier,
            )
            for chunk_text, source in self._chunk_text(content, fname):
                self.chunks.append(chunk_text)
                self.chunk_sources.append(source)
                self.chunk_tiers.append(tier)
                self.chunk_source_weights.append(doc_weight)
                self.chunk_scope_meta.append(
                    {
                        **scope_meta,
                        "source": source,
                        "tier": tier,
                    }
                )
        self._local_retriever.build(self.chunks)
        self._bm25_retriever.build(self.chunks)
        if self._dense_retriever.health().get("enabled"):
            self._dense_retriever.sync_documents(
                self.chunks,
                self.chunk_sources,
                self.chunk_tiers,
                self.chunk_source_weights,
                self.chunk_scope_meta,
            )
        self._last_build_time = time.time()
        return len(self.chunks)

    def _scope_requires_local_prefilter(self, knowledge_scope: dict | None) -> bool:
        scope = knowledge_scope if isinstance(knowledge_scope, dict) else {}
        return any(scope.get(key) for key in ("libraries", "categories", "tags", "files"))

    def _scoped_chunk_payload(self, knowledge_scope: dict | None) -> tuple[list[str], list[str], list[str], list[float]]:
        if not self._scope_requires_local_prefilter(knowledge_scope):
            return self.chunks, self.chunk_sources, self.chunk_tiers, self.chunk_source_weights
        chunks: list[str] = []
        sources: list[str] = []
        tiers: list[str] = []
        source_weights: list[float] = []
        for idx, scope_meta in enumerate(self.chunk_scope_meta):
            if not retrieval_scope_meta_matches(scope_meta, knowledge_scope):
                continue
            chunks.append(self.chunks[idx])
            sources.append(self.chunk_sources[idx])
            tiers.append(self.chunk_tiers[idx])
            source_weights.append(self.chunk_source_weights[idx])
        return chunks, sources, tiers, source_weights

    def search(
        self,
        query: str,
        top_k: int = 5,
        backend_override: str | None = None,
        knowledge_scope: dict | None = None,
    ) -> list[dict]:
        """按当前检索后端执行搜索。"""
        backend = self._select_backend(backend_override=backend_override)
        local_prefilter = self._scope_requires_local_prefilter(knowledge_scope)
        scoped_chunks, scoped_sources, scoped_tiers, scoped_source_weights = self._scoped_chunk_payload(knowledge_scope)
        if local_prefilter and backend in {"hybrid", "dense"}:
            backend = "bm25" if self._bm25_retriever.health().get("ready") else "local_tfidf"
        bm25_retriever = self._bm25_retriever
        local_retriever = self._local_retriever
        if local_prefilter:
            bm25_retriever = BM25Retriever(config_data=self._retrieval_cfg())
            bm25_retriever.build(scoped_chunks)
            local_retriever = LocalTfidfRetriever()
            local_retriever.build(scoped_chunks)
        candidate_limit = self._candidate_limit_for_config(top_k)
        results: list[dict]
        if backend == "hybrid":
            dense_results = []
            if self._dense_retriever.is_ready():
                dense_results = self._dense_retriever.search(query, top_k=candidate_limit, knowledge_scope=knowledge_scope)
            sparse_results = bm25_retriever.search(
                query=query,
                chunks=scoped_chunks,
                chunk_sources=scoped_sources,
                chunk_tiers=scoped_tiers,
                chunk_source_weights=scoped_source_weights,
                top_k=candidate_limit,
                tier_config=self._tier_cfg(),
            )
            results = self._hybrid_retriever.search(
                query=query,
                dense_results=dense_results,
                sparse_results=sparse_results,
                top_k=candidate_limit,
            )
        elif backend == "dense":
            dense_results = self._dense_retriever.search(query, top_k=candidate_limit, knowledge_scope=knowledge_scope)
            if dense_results:
                results = dense_results
            else:
                results = []
        elif backend == "bm25":
            results = bm25_retriever.search(
                query=query,
                chunks=scoped_chunks,
                chunk_sources=scoped_sources,
                chunk_tiers=scoped_tiers,
                chunk_source_weights=scoped_source_weights,
                top_k=candidate_limit,
                tier_config=self._tier_cfg(),
            )
        else:
            results = []
        if not results:
            results = local_retriever.search(
                query=query,
                chunks=scoped_chunks,
                chunk_sources=scoped_sources,
                chunk_tiers=scoped_tiers,
                chunk_source_weights=scoped_source_weights,
                top_k=candidate_limit,
                tier_config=self._tier_cfg(),
            )
        return rerank_candidates(
            query=query,
            candidates=results,
            top_k=top_k,
            config_data=self._retrieval_cfg(),
        )

    def as_langchain_retriever(
        self,
        *,
        top_k: int = 5,
        backend_override: str | None = None,
        compressed: bool = True,
        max_chars_per_doc: int = 900,
    ):
        """把当前 RAG 引擎导出为 LangChain Retriever。

        这样后面无论是接 compression chain、tool 调用还是更复杂的 LangChain 组件，
        都可以先复用现有检索能力，而不是重写一套检索适配层。
        """
        return build_langchain_retrieval_stack(
            lambda query, limit: self.search(query=query, top_k=limit, backend_override=backend_override),
            top_k=top_k,
            compressed=compressed,
            max_chars_per_doc=max_chars_per_doc,
        )

    def get_stats(self) -> dict:
        """返回知识库统计和检索后端状态。"""
        knowledge_tiers = self._tier_cfg()
        tier_counts = {}
        for tier_name, tier_cfg in knowledge_tiers.items():
            count = sum(1 for tier in self.chunk_tiers if tier == tier_name)
            tier_counts[tier_name] = {
                "label": tier_cfg["label"],
                "desc": tier_cfg["desc"],
                "weight": tier_cfg["weight"],
                "chunks": count,
            }
        return {
            "total_chunks": len(self.chunks),
            "is_ready": bool(self.chunks),
            "last_build_time": self._last_build_time,
            "tiers": tier_counts,
            "retrieval_backend": self._select_backend(),
            "retrieval_backends": {
                "local_tfidf": self._local_retriever.health(),
                "bm25": self._bm25_retriever.health(),
                "dense": self._dense_retriever.health(),
                "hybrid": self._hybrid_retriever.health(),
            },
            "rerank": reranker_service.health(),
        }

    def get_tier_files(self) -> dict:
        """按层级列出文件。"""
        result = {}
        knowledge_dir = self._runtime_knowledge_dir()
        knowledge_tiers = self._tier_cfg()
        for tier_name in knowledge_tiers:
            tier_dir = os.path.join(knowledge_dir, tier_name)
            files = []
            if os.path.isdir(tier_dir):
                for f in os.listdir(tier_dir):
                    if not f.endswith(".md"):
                        continue
                    fpath = os.path.join(tier_dir, f)
                    preview = ""
                    try:
                        with open(fpath, "r", encoding="utf-8") as handle:
                            in_excerpt = False
                            for line in handle:
                                stripped = line.strip()
                                if stripped.startswith("## 内容摘录"):
                                    in_excerpt = True
                                    continue
                                cleaned = stripped.lstrip("#- ").strip()
                                if cleaned:
                                    if not in_excerpt and cleaned in ("元数据", "可回答问题"):
                                        continue
                                    preview = cleaned[:80]
                                    if in_excerpt:
                                        break
                    except Exception:
                        preview = ""
                    files.append(
                        {
                            "name": f,
                            "size": os.path.getsize(fpath),
                            "mtime": os.path.getmtime(fpath),
                            "preview": preview,
                        }
                    )
            result[tier_name] = files
        return result


rag_engine = RAGEngine()


def build_runtime_rag_engine(
    *,
    knowledge_dir: str,
    app_config: dict,
    retrieval_config: dict,
) -> RAGEngine:
    """构建运行期 RAG 引擎。

    平台总后台和每个租户都需要自己的知识目录、检索配置和层级权重，
    这里统一返回一份独立实例，避免所有请求共享平台默认配置。
    """
    engine = RAGEngine(
        knowledge_dir=knowledge_dir,
        app_config=app_config,
        retrieval_config=retrieval_config,
        knowledge_namespace=str(app_config.get("knowledge_namespace") or ""),
    )
    engine.build_index()
    return engine
