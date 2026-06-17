"""LangChain 组件层。

集中管理四类能力：
1. 文档加载器工厂：把 PDF / Word / PPT / HTML / Markdown 的加载入口统一起来。
2. 文档切块器：把标题分块、递归分块统一收口，避免业务代码重复判断。
3. 检索器适配层：把当前自研检索结果包装成 LangChain Retriever 可消费的形式。
4. 轻量压缩检索层：对初次召回结果做查询相关压缩，减少无关上下文噪声。

这样做的目的不是“为了用 LangChain 而用”，而是把项目里最容易继续扩展的能力
抽成一层稳定接口，后面增强文档处理、混合检索、工具调用都会更顺。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable
import re

from bs4 import BeautifulSoup

try:
    from langchain_core.documents import Document
except Exception:  # pragma: no cover
    @dataclass
    class Document:  # type: ignore[override]
        page_content: str
        metadata: dict

try:
    from langchain_core.retrievers import BaseRetriever
except Exception:  # pragma: no cover
    BaseRetriever = object  # type: ignore[assignment]

try:
    from langchain_text_splitters import MarkdownHeaderTextSplitter, RecursiveCharacterTextSplitter
except Exception:  # pragma: no cover
    MarkdownHeaderTextSplitter = None
    RecursiveCharacterTextSplitter = None

try:
    from langchain_community.document_loaders import (
        BSHTMLLoader,
        Docx2txtLoader,
        PyPDFLoader,
        TextLoader,
        UnstructuredExcelLoader,
        UnstructuredHTMLLoader,
        UnstructuredPDFLoader,
        UnstructuredPowerPointLoader,
        UnstructuredWordDocumentLoader,
    )
except Exception:  # pragma: no cover
    BSHTMLLoader = None
    Docx2txtLoader = None
    PyPDFLoader = None
    TextLoader = None
    UnstructuredExcelLoader = None
    UnstructuredHTMLLoader = None
    UnstructuredPDFLoader = None
    UnstructuredPowerPointLoader = None
    UnstructuredWordDocumentLoader = None


def langchain_runtime_status() -> dict:
    """返回当前 LangChain 组件可用性，便于后续排障和日志展示。"""
    return {
        "documents": Document is not None,
        "retriever_base": BaseRetriever is not object,
        "header_splitter": MarkdownHeaderTextSplitter is not None,
        "recursive_splitter": RecursiveCharacterTextSplitter is not None,
        "pdf_loader": PyPDFLoader is not None or UnstructuredPDFLoader is not None,
        "word_loader": UnstructuredWordDocumentLoader is not None or Docx2txtLoader is not None,
        "ppt_loader": UnstructuredPowerPointLoader is not None,
        "html_loader": UnstructuredHTMLLoader is not None or BSHTMLLoader is not None,
        "excel_loader": UnstructuredExcelLoader is not None,
        "text_loader": TextLoader is not None,
    }


def _read_text_fallback(path: Path) -> str:
    for encoding in ("utf-8", "utf-8-sig", "gb18030"):
        try:
            return path.read_text(encoding=encoding)
        except Exception:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


def _html_fallback_to_document(path: Path) -> list[Document]:
    raw_html = _read_text_fallback(path)
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""
    lines: list[str] = []
    if title:
        lines.extend([f"# {title}", ""])
    for node in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr"]):
        text = " ".join(node.get_text(" ", strip=True).split())
        if not text:
            continue
        name = node.name.lower()
        if name.startswith("h") and len(name) == 2 and name[1].isdigit():
            level = min(max(int(name[1]), 1), 6)
            lines.append(f"{'#' * level} {text}")
        elif name == "li":
            lines.append(f"- {text}")
        elif name == "tr":
            lines.append(f"| {text} |")
        else:
            lines.append(text)
        lines.append("")
    return [Document(page_content="\n".join(lines).strip(), metadata={"source": str(path), "loader": "html_fallback"})]


def load_documents_with_langchain(path: Path) -> list[Document]:
    """统一的 LangChain 文档加载入口。

    注意：
    - 这里只做“标准化入口”；
    - OCR、表格抽取、跨页修复仍由业务层追加，因为那是企业文档增强逻辑，
      不应该被 LangChain 原始 loader 完全替代。
    """
    suffix = path.suffix.lower()
    loader_candidates: list[Callable[[], list[Document]]] = []

    if suffix == ".pdf":
        if PyPDFLoader is not None:
            loader_candidates.append(lambda: PyPDFLoader(str(path)).load())
        if UnstructuredPDFLoader is not None:
            loader_candidates.append(lambda: UnstructuredPDFLoader(str(path), mode="elements", strategy="hi_res").load())
            loader_candidates.append(lambda: UnstructuredPDFLoader(str(path), mode="elements").load())
    elif suffix == ".docx":
        if UnstructuredWordDocumentLoader is not None:
            loader_candidates.append(lambda: UnstructuredWordDocumentLoader(str(path), mode="elements").load())
        if Docx2txtLoader is not None:
            loader_candidates.append(lambda: Docx2txtLoader(str(path)).load())
    elif suffix == ".pptx":
        if UnstructuredPowerPointLoader is not None:
            loader_candidates.append(lambda: UnstructuredPowerPointLoader(str(path)).load())
    elif suffix in {".xlsx", ".xls"}:
        if UnstructuredExcelLoader is not None:
            loader_candidates.append(lambda: UnstructuredExcelLoader(str(path)).load())
    elif suffix in {".html", ".htm"}:
        if UnstructuredHTMLLoader is not None:
            loader_candidates.append(lambda: UnstructuredHTMLLoader(str(path), mode="elements").load())
        if BSHTMLLoader is not None:
            loader_candidates.append(lambda: BSHTMLLoader(str(path)).load())
        loader_candidates.append(lambda: _html_fallback_to_document(path))
    elif suffix in {".md", ".txt"}:
        if TextLoader is not None:
            loader_candidates.append(lambda: TextLoader(str(path), encoding="utf-8").load())
        loader_candidates.append(lambda: [Document(page_content=_read_text_fallback(path), metadata={"source": str(path), "loader": "text_fallback"})])

    for candidate in loader_candidates:
        try:
            docs = candidate()
        except Exception:
            continue
        normalized = [
            Document(
                page_content=str(getattr(doc, "page_content", "") or "").strip(),
                metadata=dict(getattr(doc, "metadata", {}) or {}),
            )
            for doc in docs or []
            if str(getattr(doc, "page_content", "") or "").strip()
        ]
        if normalized:
            return normalized
    return []


def split_markdown_with_langchain(markdown_text: str) -> list[Document]:
    """统一的 LangChain 分块入口，优先走标题分块，再走递归分块。"""
    text = str(markdown_text or "").replace("\r", "").strip()
    if not text:
        return []

    docs: list[Document]
    if MarkdownHeaderTextSplitter is not None:
        splitter = MarkdownHeaderTextSplitter(
            headers_to_split_on=[("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]
        )
        docs = splitter.split_text(text)
        docs = [
            Document(
                page_content=str(getattr(doc, "page_content", "") or "").strip(),
                metadata={k: v for k, v in dict(getattr(doc, "metadata", {}) or {}).items() if v},
            )
            for doc in docs
            if str(getattr(doc, "page_content", "") or "").strip()
        ]
    else:
        docs = [Document(page_content=text, metadata={})]

    if RecursiveCharacterTextSplitter is None:
        return docs

    text_len = len(text)
    if text_len <= 1200:
        chunk_size = 720
        chunk_overlap = 60
    elif text_len <= 4000:
        chunk_size = 860
        chunk_overlap = 90
    elif text_len <= 9000:
        chunk_size = 960
        chunk_overlap = 120
    else:
        chunk_size = 1080
        chunk_overlap = 150

    recursive = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n## ", "\n### ", "\n\n", "\n", "。", "；", "，", " "],
    )
    final_docs: list[Document] = []
    for doc in docs:
        final_docs.extend(recursive.split_documents([doc]))
    return [
        Document(
            page_content=str(getattr(doc, "page_content", "") or "").strip(),
            metadata=dict(getattr(doc, "metadata", {}) or {}),
        )
        for doc in final_docs
        if str(getattr(doc, "page_content", "") or "").strip()
    ]


def _query_terms(query: str) -> list[str]:
    """提取轻量查询词，给压缩检索器复用。"""
    tokens = []
    for token in re.split(r"[\s,，。；;、:：!?！？()\[\]{}<>]+", str(query or "").strip().lower()):
        token = token.strip()
        if len(token) >= 2:
            tokens.append(token)
    return tokens


def _compress_text_for_query(text: str, query: str, *, max_chars: int = 900) -> str:
    """按查询词压缩召回内容，尽量保留最相关段落和表格。"""
    raw = str(text or "").replace("\r", "").strip()
    if not raw:
        return ""
    if len(raw) <= max_chars:
        return raw

    terms = _query_terms(query)
    blocks = [block.strip() for block in re.split(r"\n{2,}", raw) if block.strip()]
    if not blocks:
        return raw[:max_chars]

    scored_blocks: list[tuple[int, int, str]] = []
    for index, block in enumerate(blocks):
        lower_block = block.lower()
        score = 0
        for term in terms:
            if term in lower_block:
                score += 3
        if block.startswith("|") or "\n|" in block:
            score += 2
        if block.startswith("#"):
            score += 1
        scored_blocks.append((score, index, block))

    # 没有任何命中时，保留前后文的头部结构信息。
    if all(score <= 0 for score, _, _ in scored_blocks):
        return "\n\n".join(blocks[:2])[:max_chars]

    scored_blocks.sort(key=lambda item: (-item[0], item[1]))
    selected: list[tuple[int, str]] = []
    total = 0
    for score, index, block in scored_blocks:
        if score <= 0:
            continue
        estimated = len(block) + (2 if selected else 0)
        if total + estimated > max_chars and selected:
            continue
        selected.append((index, block))
        total += estimated
        if total >= max_chars:
            break

    selected.sort(key=lambda item: item[0])
    compact = "\n\n".join(block for _, block in selected).strip()
    return compact[:max_chars] if compact else raw[:max_chars]


class SearchResultRetriever(BaseRetriever):
    """把当前项目的检索结果适配为 LangChain Retriever。

    这样后面无论接 compression、工具链还是更复杂的 pipeline，
    都可以先消费统一的 `Document` 输出，而不用直接依赖我们自己的 dict 结构。
    """

    search_callable: Callable[[str, int], list[dict]]
    top_k: int = 5

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:  # type: ignore[override]
        results = self.search_callable(query, self.top_k)
        docs: list[Document] = []
        for item in results or []:
            metadata = {
                "source": str(item.get("source") or ""),
                "tier": str(item.get("tier") or ""),
                "backend": str(item.get("backend") or ""),
                "score": float(item.get("score") or 0.0),
                "weighted_score": float(item.get("weighted_score") or 0.0),
            }
            docs.append(Document(page_content=str(item.get("content") or ""), metadata=metadata))
        return docs


class QueryFocusedCompressionRetriever(BaseRetriever):
    """轻量压缩检索器。

    这一层不替代真正的 rerank / compression model，而是先把召回内容压缩成
    “对当前问题最相关的段落”，减少上下文噪声，方便后续 chain 或 prompt 直接消费。
    """

    base_retriever: BaseRetriever
    max_chars_per_doc: int = 900

    def _get_relevant_documents(self, query: str, *, run_manager=None) -> list[Document]:  # type: ignore[override]
        base_docs = self.base_retriever.get_relevant_documents(query)
        compressed_docs: list[Document] = []
        for doc in base_docs:
            content = str(getattr(doc, "page_content", "") or "")
            metadata = dict(getattr(doc, "metadata", {}) or {})
            compressed_content = _compress_text_for_query(
                content,
                query,
                max_chars=self.max_chars_per_doc,
            )
            metadata["compressed"] = True
            metadata["original_length"] = len(content)
            metadata["compressed_length"] = len(compressed_content)
            compressed_docs.append(
                Document(
                    page_content=compressed_content,
                    metadata=metadata,
                )
            )
        return compressed_docs


def build_langchain_retriever(search_callable: Callable[[str, int], list[dict]], top_k: int = 5) -> SearchResultRetriever:
    """构建统一的 LangChain Retriever 适配器。"""
    return SearchResultRetriever(search_callable=search_callable, top_k=top_k)


def build_langchain_retrieval_stack(
    search_callable: Callable[[str, int], list[dict]],
    *,
    top_k: int = 5,
    compressed: bool = True,
    max_chars_per_doc: int = 900,
):
    """构建更完整的 LangChain 检索栈。

    默认返回“召回 + 轻量压缩”组合，方便后续继续接：
    - contextual compression
    - tool 调用前的上下文压缩
    - answer synthesis chain
    """
    retriever = build_langchain_retriever(search_callable, top_k=top_k)
    if not compressed:
        return retriever
    return QueryFocusedCompressionRetriever(
        base_retriever=retriever,
        max_chars_per_doc=max_chars_per_doc,
    )
