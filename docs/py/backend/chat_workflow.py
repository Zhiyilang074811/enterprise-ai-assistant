"""LangGraph 编排的聊天主链路。"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Literal, TypedDict

from backend.app_config import load_app_config
from backend.cache import semantic_cache
from backend.database import record_chat_log
from backend.guardrails import apply_input_guardrails, apply_output_guardrails
from backend.knowledge_assets import annotate_retrieval_results_with_scope
from backend.llm_service import stream_chat_completion
from backend.memory import build_short_term_memory
from backend.model_config import load_model_config
from backend.prompt_config import load_system_prompt_template
from backend.rag import rag_engine
from backend.retrieval_orchestration import (
    build_retry_stages,
    choose_retrieval_route,
    get_retry_plan,
    judge_retrieval_quality,
    rewrite_query,
)
from backend.tools import run_tool_from_question

try:
    from langgraph.graph import END, START, StateGraph

    LANGGRAPH_AVAILABLE = True
except Exception:  # pragma: no cover - 允许依赖未安装时回退
    END = "__end__"
    START = "__start__"
    StateGraph = None
    LANGGRAPH_AVAILABLE = False


class ChatWorkflowState(TypedDict, total=False):
    question: str
    images: list[dict]
    cache_key: str
    phone: str
    tenant_id: str
    agent_id: str
    session_id: str
    request_id: str
    # 运行时依赖必须显式进入 LangGraph 状态，否则租户链路会退回平台默认配置。
    rag_runtime: Any
    app_loader: Callable[[], dict]
    model_loader: Callable[[], dict]
    prompt_loader: Callable[[], str]
    llm_context: dict[str, Any]
    rag_results: list[dict]
    knowledge_context: str
    memory_context: str
    retrieval_backend: str
    prompt_template: str
    system_prompt: str
    app_settings: dict
    model_settings: dict
    cache_hit: bool
    cached_answer: str
    cached_knowledge_hits: list[dict]
    cached_retrieval_trace: dict[str, Any]
    blocked: bool
    block_message: str
    guardrail_events: list[dict]
    original_question: str
    rewritten_question: str
    rewrite_applied: bool
    rewrite_notes: list[str]
    query_profile: str
    matched_entities: list[str]
    query_intent: str
    answer_strategy: str
    retrieval_route: dict[str, Any]
    retrieval_judge: dict[str, Any]
    retrieval_attempts: int
    retrieval_strategy_trace: list[dict[str, Any]]
    tool_result: dict[str, Any]
    tool_used: str
    knowledge_scope: dict[str, Any]
    allowed_tools: list[str]
    mcp_servers: list[str]
    skip_cache_lookup: bool
    skip_cache_write: bool
    phase_timings: dict[str, Any]
    model_route: list[str]
    base_url: str
    answer_events: list[str]
    answer_text: str
    selected_model: str
    llm_error_message: str
    llm_context: dict
    log_chat: bool


def _input_guard_node(state: ChatWorkflowState) -> ChatWorkflowState:
    result = apply_input_guardrails(state.get("question", ""))
    return {
        **state,
        "question": result.get("text", state.get("question", "")),
        "blocked": not result.get("ok", True),
        "block_message": result.get("message", ""),
        "guardrail_events": result.get("events", []),
    }


def _cache_lookup_node(state: ChatWorkflowState) -> ChatWorkflowState:
    started_at = time.perf_counter()
    if state.get("skip_cache_lookup"):
        return {
            **state,
            "cache_hit": False,
            "cached_answer": "",
            "cached_knowledge_hits": [],
            "cached_retrieval_trace": {},
            "phase_timings": {
                **dict(state.get("phase_timings") or {}),
                "cache_lookup_ms": round((time.perf_counter() - started_at) * 1000, 2),
                "cache_lookup_skipped": True,
            },
        }
    cache_key = state.get("cache_key") or state.get("question", "")
    cached = semantic_cache.get(cache_key)
    app_loader = state.get("app_loader")
    app_settings = state.get("app_settings")
    if cached and not isinstance(app_settings, dict):
        app_settings = app_loader() if callable(app_loader) else load_app_config()
    return {
        **state,
        "cache_hit": bool(cached),
        "cached_answer": str((cached or {}).get("answer") or ""),
        "cached_knowledge_hits": list((cached or {}).get("knowledge_hits") or []),
        "cached_retrieval_trace": dict((cached or {}).get("retrieval_trace") or {}),
        "app_settings": app_settings,
        "phase_timings": {
            **dict(state.get("phase_timings") or {}),
            "cache_lookup_ms": round((time.perf_counter() - started_at) * 1000, 2),
            "cache_hit": bool(cached),
        },
    }


def _query_rewrite_node(state: ChatWorkflowState) -> ChatWorkflowState:
    """对问题做轻量改写，提高企业场景召回稳定性。"""
    rag_runtime = state.get("rag_runtime") or rag_engine
    retrieval_config = rag_runtime._retrieval_cfg() if hasattr(rag_runtime, "_retrieval_cfg") else None
    route = choose_retrieval_route(
        state.get("question", ""),
        retrieval_config,
        preferred_backend=rag_runtime.get_stats().get("retrieval_backend", "hybrid"),
    )
    result = rewrite_query(
        state.get("question", ""),
        retrieval_config,
        profile=route.get("profile"),
        attempt=1,
        mode="normal",
    )
    rewritten = str(result.get("rewritten") or state.get("question", "")).strip()
    query_intent = str(route.get("intent") or "knowledge")
    answer_strategy = str(route.get("answer_strategy") or "knowledge_rag")
    return {
        **state,
        "original_question": state.get("question", ""),
        "question": rewritten or state.get("question", ""),
        "rewritten_question": rewritten or state.get("question", ""),
        "rewrite_applied": bool(result.get("applied")),
        "rewrite_notes": list(result.get("notes") or []),
        "matched_entities": list(result.get("matched_entities") or []),
        "query_profile": str(result.get("profile") or route.get("profile") or "keyword_exact"),
        "query_intent": query_intent,
        "answer_strategy": answer_strategy,
        "retrieval_route": route,
    }


def _tool_node(state: ChatWorkflowState) -> ChatWorkflowState:
    """在检索前优先执行工具调用。"""
    app_loader = state.get("app_loader")
    app_settings = state.get("app_settings")
    if not isinstance(app_settings, dict):
        app_settings = app_loader() if callable(app_loader) else load_app_config()
    tenant_name = str(app_settings.get("app_name") or state.get("tenant_id") or "")
    result = run_tool_from_question(
        state.get("original_question") or state.get("question", ""),
        tenant_id=state.get("tenant_id"),
        tenant_name=tenant_name,
        allowed_tools=state.get("allowed_tools") or [],
        allowed_mcp_servers=state.get("mcp_servers") or [],
    )
    if result.get("matched") and result.get("ok"):
        answer_text = str(result.get("message") or "").strip()
        answer_events = [f"data: {json.dumps({'content': answer_text}, ensure_ascii=False)}\n\n", "data: [DONE]\n\n"]
        return {
            **state,
            "app_settings": app_settings,
            "tool_result": result,
            "tool_used": str(result.get("tool") or ""),
            "answer_text": answer_text,
            "answer_events": answer_events,
            "skip_cache_write": bool(result.get("skip_cache", True)),
        }
    return {
        **state,
        "app_settings": app_settings,
        "tool_result": result,
        "tool_used": str(result.get("tool") or ""),
        "skip_cache_write": False,
    }


def _retrieve_node(state: ChatWorkflowState) -> ChatWorkflowState:
    started_at = time.perf_counter()
    question = state.get("question", "")
    rag_runtime = state.get("rag_runtime") or rag_engine
    retrieval_config = rag_runtime._retrieval_cfg() if hasattr(rag_runtime, "_retrieval_cfg") else None
    retry_plan = get_retry_plan(retrieval_config)
    route = state.get("retrieval_route") or choose_retrieval_route(
        question,
        retrieval_config,
        preferred_backend=rag_runtime.get_stats().get("retrieval_backend", "hybrid"),
    )
    top_k = 5
    scoped_top_k = max(top_k, 12) if state.get("knowledge_scope") else top_k
    backend = str(route.get("backend") or "hybrid")
    rag_results = annotate_retrieval_results_with_scope(
        tenant_id=state.get("tenant_id", "default"),
        results=rag_runtime.search(
            question,
            top_k=scoped_top_k,
            backend_override=backend,
            knowledge_scope=state.get("knowledge_scope") or {},
        ),
        knowledge_scope=state.get("knowledge_scope") or {},
    )[:top_k]
    judge = judge_retrieval_quality(rag_results, retrieval_config)
    attempts = 1
    strategy_trace = [
        {
            "attempt": 1,
            "backend": backend,
            "top_k": top_k,
            "profile": route.get("profile"),
            "judge": judge,
            "query": question,
            "strategy": route.get("strategy"),
        }
    ]
    if not judge.get("ok") and retry_plan.get("enabled"):
        max_attempts = int(retry_plan.get("max_attempts", 1) or 1)
        for stage in build_retry_stages(
            question,
            retrieval_config,
            preferred_backend=backend,
        ):
            if attempts >= max_attempts or judge.get("ok"):
                break
            rewritten = rewrite_query(
                question,
                retrieval_config,
                profile=route.get("profile"),
                attempt=int(stage.get("attempt") or attempts + 1),
                mode=str(stage.get("rewrite_mode") or "normal"),
            )
            stage_query = str(rewritten.get("rewritten") or question).strip() or question
            stage_backend = str(stage.get("backend") or backend)
            stage_top_k = int(stage.get("top_k") or retry_plan.get("fallback_top_k", top_k) or top_k)
            stage_requested_top_k = max(stage_top_k, 12) if state.get("knowledge_scope") else stage_top_k
            rag_results = annotate_retrieval_results_with_scope(
                tenant_id=state.get("tenant_id", "default"),
                results=rag_runtime.search(
                    stage_query,
                    top_k=stage_requested_top_k,
                    backend_override=stage_backend,
                    knowledge_scope=state.get("knowledge_scope") or {},
                ),
                knowledge_scope=state.get("knowledge_scope") or {},
            )[:stage_top_k]
            judge = judge_retrieval_quality(rag_results, retrieval_config)
            attempts += 1
            strategy_trace.append(
                {
                    "attempt": attempts,
                    "backend": stage_backend,
                    "top_k": stage_top_k,
                    "profile": route.get("profile"),
                    "judge": judge,
                    "query": stage_query,
                    "strategy": stage.get("strategy"),
                    "rewrite_mode": stage.get("rewrite_mode"),
                }
            )
    retrieval_backend = rag_results[0].get("backend", "") if rag_results else backend
    context_parts: list[str] = []
    for index, item in enumerate(rag_results):
        tier_label = item.get("tier_label", "未知")
        context_parts.append(
            f"[知识片段{index + 1}] (来源: {item.get('source', '-')}, 层级: {tier_label}, 相关度: {item.get('score', 0):.2f})\n"
            f"{item.get('content', '')}"
        )
    return {
        **state,
        "rag_results": rag_results,
        "retrieval_backend": retrieval_backend,
        "retrieval_judge": judge,
        "retrieval_attempts": attempts,
        "retrieval_route": route,
        "retrieval_strategy_trace": strategy_trace,
        "knowledge_context": "\n\n---\n\n".join(context_parts) if context_parts else "（知识库中未找到相关内容）",
        "phase_timings": {
            **dict(state.get("phase_timings") or {}),
            "retrieve_ms": round((time.perf_counter() - started_at) * 1000, 2),
        },
    }


def _memory_node(state: ChatWorkflowState) -> ChatWorkflowState:
    """从最近几轮聊天日志中构建短期记忆。"""
    started_at = time.perf_counter()
    app_loader = state.get("app_loader")
    app_settings = state.get("app_settings")
    if not isinstance(app_settings, dict):
        app_settings = app_loader() if callable(app_loader) else load_app_config()
    memory_context = build_short_term_memory(
        phone=state.get("phone", ""),
        tenant_id=state.get("tenant_id", "default"),
        agent_id=state.get("agent_id", ""),
        session_id=state.get("session_id", ""),
        app_settings=app_settings,
    )
    return {
        **state,
        "app_settings": app_settings,
        "memory_context": memory_context,
        "phase_timings": {
            **dict(state.get("phase_timings") or {}),
            "memory_ms": round((time.perf_counter() - started_at) * 1000, 2),
        },
    }


def _prompt_build_node(state: ChatWorkflowState) -> ChatWorkflowState:
    started_at = time.perf_counter()
    prompt_loader = state.get("prompt_loader")
    prompt_template = prompt_loader() if callable(prompt_loader) else load_system_prompt_template()
    knowledge_context = state.get("knowledge_context", "（知识库中未找到相关内容）")
    app_loader = state.get("app_loader")
    model_loader = state.get("model_loader")
    memory_context = str(state.get("memory_context") or "").strip()
    memory_block = ""
    answer_strategy = str(state.get("answer_strategy") or "knowledge_rag")
    query_intent = str(state.get("query_intent") or "knowledge")
    retrieval_judge = state.get("retrieval_judge") or {}
    matched_entities = list(state.get("matched_entities") or [])
    route_block = ""
    if answer_strategy == "knowledge_rag":
        route_block = (
            "\n\n## 回答策略\n"
            "这是一道企业知识问题。若知识库已有明确依据，请优先依据知识库回答；"
            "若依据不足，可说明知识库依据有限，但不要编造企业内部事实。"
        )
    elif answer_strategy == "general_fallback":
        route_block = (
            "\n\n## 回答策略\n"
            "这是一道通用问题。若知识库命中不足，可基于通用模型能力直接回答；"
            "但不要把通用常识表述成企业内部制度或正式口径。"
        )
    elif answer_strategy == "realtime_fallback":
        route_block = (
            "\n\n## 回答策略\n"
            "这是一道实时信息问题。请先参考已有知识库；若知识库不足且未命中可用工具，"
            "可以给出谨慎的通用帮助，但要明确区分：你无法联网确认实时外部信息。"
        )
    elif answer_strategy == "tool_first":
        route_block = (
            "\n\n## 回答策略\n"
            "这是一道工具型问题。若工具未触发成功，再结合知识库和通用能力给出最简洁的兜底说明。"
        )
    strategy_context = (
        f"\n\n## 路由上下文\n"
        f"- 问题意图：{query_intent}\n"
        f"- 回答策略：{answer_strategy}\n"
        f"- 检索画像：{state.get('query_profile', '')}\n"
        f"- 检索质量：{retrieval_judge.get('confidence_band', '-')}\n"
        f"- 命中实体：{', '.join(matched_entities) if matched_entities else '无'}"
    )
    if memory_context:
        memory_block = f"\n\n## 会话短期记忆\n以下是当前用户最近几轮真实对话，请在不偏离知识库依据的前提下保持上下文连续：\n{memory_context}"
    return {
        **state,
        "prompt_template": prompt_template,
        "system_prompt": prompt_template.replace("{knowledge_context}", knowledge_context) + route_block + strategy_context + memory_block,
        "app_settings": app_loader() if callable(app_loader) else load_app_config(),
        "model_settings": model_loader() if callable(model_loader) else load_model_config(),
        "phase_timings": {
            **dict(state.get("phase_timings") or {}),
            "prompt_build_ms": round((time.perf_counter() - started_at) * 1000, 2),
        },
    }


def _model_route_node(state: ChatWorkflowState) -> ChatWorkflowState:
    model_settings = state.get("model_settings") or load_model_config()
    providers = model_settings.get("providers") if isinstance(model_settings, dict) else None
    provider_routes: list[dict] = []
    if isinstance(providers, list):
        for index, item in enumerate(providers):
            if not isinstance(item, dict):
                continue
            primary_model = str(item.get("model_primary") or item.get("model") or "").strip()
            fallback_model = str(item.get("model_fallback") or "").strip()
            route = [value for value in [primary_model, fallback_model] if value]
            deduped_route: list[str] = []
            for value in route:
                if value not in deduped_route:
                    deduped_route.append(value)
            provider_routes.append(
                {
                    "provider_id": str(item.get("id") or f"provider_{index + 1}"),
                    "provider_label": str(item.get("label") or item.get("name") or f"供应商 {index + 1}"),
                    "base_url": str(item.get("base_url") or "").strip(),
                    "supports_image": bool(item.get("supports_image")),
                    "api_keys": list(item.get("api_keys") or []),
                    "model_route": deduped_route,
                }
            )

    first_route = provider_routes[0]["model_route"] if provider_routes else []
    return {
        **state,
        "model_route": first_route,
        "provider_routes": provider_routes,
        "base_url": str(model_settings.get("base_url") or ""),
    }


async def _generate_answer_node(state: ChatWorkflowState) -> ChatWorkflowState:
    """在工作流里执行真实的 LLM 生成。

    这样聊天主链路的生成阶段不再挂在主路由里，而是和检索、路由统一由 LangGraph 编排。
    """
    llm_context = state.get("llm_context") or {}
    event_lines: list[str] = []
    emitted_chunks: list[str] = []
    output_events: list[dict] = list(state.get("guardrail_events") or [])
    llm_runtime = {"selected_model": "", "error_message": ""}
    started_at = time.perf_counter()
    first_token_ms: float | None = None

    async for line in stream_chat_completion(
        question=state.get("question", ""),
        images=state.get("images") or [],
        system_prompt=state.get("system_prompt", ""),
        model_settings=state.get("model_settings") or load_model_config(),
        workflow_route=state.get("model_route") or [],
        provider_routes=state.get("provider_routes") or [],
        default_base_url=str(llm_context.get("default_base_url") or ""),
        ssl_ctx=llm_context.get("ssl_ctx"),
        on_model_selected=lambda model: llm_runtime.__setitem__("selected_model", model),
        on_output_event=lambda event: output_events.append(event),
        on_error=lambda msg: llm_runtime.__setitem__("error_message", msg),
        protect_output=apply_output_guardrails,
        user_facing_error=llm_context.get("user_facing_error") or (lambda: "服务暂时繁忙，请稍后重试。"),
        collector=[],
    ):
        event_lines.append(line)
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            try:
                payload = json.loads(line[6:].strip())
            except Exception:
                payload = {}
            content = str(payload.get("content") or "")
            if content:
                if first_token_ms is None:
                    first_token_ms = round((time.perf_counter() - started_at) * 1000, 2)
                emitted_chunks.append(content)

    return {
        **state,
        "answer_events": event_lines,
        "answer_text": "".join(emitted_chunks),
        "selected_model": llm_runtime["selected_model"],
        "llm_error_message": llm_runtime["error_message"],
        "guardrail_events": output_events,
        "phase_timings": {
            **dict(state.get("phase_timings") or {}),
            "llm_first_token_ms": first_token_ms,
            "llm_generate_ms": round((time.perf_counter() - started_at) * 1000, 2),
        },
    }


def _trace_route_label(route: object) -> str:
    if isinstance(route, dict):
        backend = str(route.get("backend") or "").strip()
        strategy = str(route.get("strategy") or "").strip()
        profile = str(route.get("profile") or "").strip()
        parts = [item for item in [backend, strategy, profile] if item]
        return " / ".join(parts) if parts else "hybrid"
    return str(route or "").strip() or "hybrid"


def build_knowledge_hits(rag_results: list[dict] | None) -> list[dict]:
    return [
        {
            "source": item.get("source", ""),
            "file": item.get("file", ""),
            "title": item.get("title", ""),
            "tier": item.get("tier", ""),
            "tier_label": item.get("tier_label", ""),
            "library_id": item.get("library_id", ""),
            "library_name": item.get("library_name", ""),
            "category_id": item.get("category_id", ""),
            "category_name": item.get("category_name", ""),
            "file_key": item.get("file_key", ""),
            "tags": item.get("tags") or [],
            "score": item.get("score", 0),
            "rerank_score": item.get("rerank_score"),
            "final_score": item.get("final_score"),
            "content": item.get("content", ""),
            "text": item.get("text") or item.get("content", ""),
            "backend": item.get("backend", ""),
        }
        for item in (rag_results or [])
    ]


def build_retrieval_trace(
    state: dict[str, Any],
    knowledge_hits: list[dict] | None = None,
) -> dict[str, Any]:
    retrieval_route = state.get("retrieval_route") or {}
    rag_runtime = state.get("rag_runtime")
    retrieval_cfg = rag_runtime._retrieval_cfg() if hasattr(rag_runtime, "_retrieval_cfg") else {}
    embedding_cfg = dict((retrieval_cfg or {}).get("embedding") or {})
    rerank_cfg = dict((retrieval_cfg or {}).get("rerank") or {})
    hits = knowledge_hits if knowledge_hits is not None else build_knowledge_hits(state.get("rag_results"))
    hybrid_sources = sorted(
        {
            str(source).lower()
            for item in (state.get("rag_results") or [])
            for source in (item.get("hybrid_sources") or [])
            if str(source).strip()
        }
    )
    retrieval_backend = str(state.get("retrieval_backend", "") or "")
    embedding_active = retrieval_backend in {"dense", "hybrid"} or "dense" in hybrid_sources
    sparse_active = retrieval_backend in {"bm25", "hybrid"} or "sparse" in hybrid_sources
    rerank_score = next(
        (item.get("rerank_score") for item in hits if item.get("rerank_score") is not None),
        None,
    )
    return {
        "model_name": state.get("selected_model", ""),
        "selected_model": state.get("selected_model", ""),
        "query_profile": state.get("query_profile", "") or (
            retrieval_route.get("profile", "") if isinstance(retrieval_route, dict) else ""
        ),
        "rewrite_applied": bool(state.get("rewrite_applied")),
        "rewrite_notes": list(state.get("rewrite_notes") or []),
        "matched_entities": list(state.get("matched_entities") or []),
        "query_intent": str(state.get("query_intent") or ""),
        "answer_strategy": str(state.get("answer_strategy") or ""),
        "knowledge_scope": dict(state.get("knowledge_scope") or {}),
        "retrieval_route": _trace_route_label(retrieval_route),
        "retrieval_route_raw": retrieval_route,
        "retrieval_strategy": (
            retrieval_route.get("strategy", "") if isinstance(retrieval_route, dict) else ""
        ),
        "retrieval_route_reason": (
            retrieval_route.get("reason", "") if isinstance(retrieval_route, dict) else ""
        ),
        "retrieval_backend": retrieval_backend,
        "hybrid_sources": hybrid_sources,
        "embedding_provider": str(embedding_cfg.get("provider") or ""),
        "embedding_model": str(embedding_cfg.get("model") or ""),
        "embedding_active": embedding_active,
        "sparse_active": sparse_active,
        "retrieval_judge": state.get("retrieval_judge") or {},
        "retrieval_attempts": int(state.get("retrieval_attempts") or 0),
        "retrieval_strategy_trace": list(state.get("retrieval_strategy_trace") or []),
        "rerank_enabled": bool(rerank_cfg.get("enabled", True)),
        "rerank_provider": str(rerank_cfg.get("provider") or ""),
        "rerank_model": str(rerank_cfg.get("model") or ""),
        "rerank_applied": any(item.get("rerank_score") is not None for item in hits),
        "rerank_score": rerank_score,
        "hit_tiers": sorted(
            {str(item.get("tier") or "").upper() for item in hits if str(item.get("tier") or "").strip()}
        ),
        "phase_timings": dict(state.get("phase_timings") or {}),
    }


def _finalize_answer_node(state: ChatWorkflowState) -> ChatWorkflowState:
    """把缓存和聊天日志也纳入工作流。"""
    answer_text = str(state.get("answer_text") or "")
    if answer_text and not answer_text.startswith("["):
        knowledge_hits = build_knowledge_hits(state.get("rag_results"))
        retrieval_trace = build_retrieval_trace(state, knowledge_hits)
        if not state.get("skip_cache_write"):
            semantic_cache.put(
                state.get("cache_key") or state.get("question", ""),
                answer_text,
                knowledge_hits=knowledge_hits,
                retrieval_trace=retrieval_trace,
            )
        app_settings = state.get("app_settings") or {}
        if app_settings.get("record_chat_logs", True) and state.get("log_chat", True):
            record_chat_log(
                phone=state.get("phone", ""),
                question=state.get("question", ""),
                answer=answer_text,
                knowledge_hits=knowledge_hits,
                retrieval_trace=retrieval_trace,
                tenant_id=state.get("tenant_id", "default"),
                agent_id=state.get("agent_id", ""),
                request_id=state.get("request_id", ""),
                session_id=state.get("session_id", ""),
            )
    return state



def _route_after_guard(state: ChatWorkflowState) -> Literal["blocked", "continue"]:
    return "blocked" if state.get("blocked") else "continue"


def _route_after_cache(state: ChatWorkflowState) -> Literal["cached", "retrieve"]:
    return "cached" if state.get("cache_hit") else "retrieve"


def _route_after_tool(state: ChatWorkflowState) -> Literal["tool", "retrieve"]:
    return "tool" if str(state.get("answer_text") or "").strip() else "retrieve"


def _build_langgraph():
    graph = StateGraph(ChatWorkflowState)
    graph.add_node("input_guard", _input_guard_node)
    graph.add_node("cache_lookup", _cache_lookup_node)
    graph.add_node("query_rewrite", _query_rewrite_node)
    graph.add_node("tool_router", _tool_node)
    graph.add_node("memory", _memory_node)
    graph.add_node("retrieve", _retrieve_node)
    graph.add_node("prompt_build", _prompt_build_node)
    graph.add_node("route_model", _model_route_node)
    graph.add_node("generate_answer", _generate_answer_node)
    graph.add_node("finalize_answer", _finalize_answer_node)
    graph.add_edge(START, "input_guard")
    graph.add_conditional_edges(
        "input_guard",
        _route_after_guard,
        {
            "blocked": END,
            "continue": "cache_lookup",
        },
    )
    graph.add_conditional_edges(
        "cache_lookup",
        _route_after_cache,
        {
            "cached": END,
            "retrieve": "query_rewrite",
        },
    )
    graph.add_edge("query_rewrite", "tool_router")
    graph.add_conditional_edges(
        "tool_router",
        _route_after_tool,
        {
            "tool": "finalize_answer",
            "retrieve": "memory",
        },
    )
    graph.add_edge("memory", "retrieve")
    graph.add_edge("retrieve", "prompt_build")
    graph.add_edge("prompt_build", "route_model")
    graph.add_edge("route_model", "generate_answer")
    graph.add_edge("generate_answer", "finalize_answer")
    graph.add_edge("finalize_answer", END)
    return graph.compile()


def _run_fallback(initial_state: ChatWorkflowState) -> ChatWorkflowState:
    state = _input_guard_node(initial_state)
    if state.get("blocked"):
        return state
    state = _cache_lookup_node(state)
    if state.get("cache_hit"):
        return state
    state = _query_rewrite_node(state)
    state = _tool_node(state)
    if str(state.get("answer_text") or "").strip():
        return _finalize_answer_node(state)
    state = _memory_node(state)
    state = _retrieve_node(state)
    state = _prompt_build_node(state)
    state = _model_route_node(state)
    return state


_compiled_graph = _build_langgraph() if LANGGRAPH_AVAILABLE and StateGraph is not None else None


async def run_chat_workflow(
    question: str,
    phone: str,
    images: list[dict] | None = None,
    cache_key: str | None = None,
    tenant_id: str = "default",
    agent_id: str = "",
    session_id: str = "",
    request_id: str = "",
    llm_context: dict | None = None,
    skip_cache_lookup: bool = False,
    skip_cache_write: bool = False,
) -> dict[str, Any]:
    """运行聊天主链路，LangGraph 可用时走编排框架。"""
    return await run_chat_workflow_with_runtime(
        question=question,
        images=images,
        phone=phone,
        cache_key=cache_key,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        request_id=request_id,
        llm_context=llm_context,
        skip_cache_lookup=skip_cache_lookup,
        skip_cache_write=skip_cache_write,
    )


async def run_chat_workflow_with_runtime(
    *,
    question: str,
    images: list[dict] | None = None,
    phone: str,
    cache_key: str | None = None,
    tenant_id: str = "default",
    agent_id: str = "",
    session_id: str = "",
    request_id: str = "",
    rag_runtime=None,
    app_loader: Callable[[], dict] | None = None,
    model_loader: Callable[[], dict] | None = None,
    prompt_loader: Callable[[], str] | None = None,
    llm_context: dict | None = None,
    log_chat: bool = True,
    skip_cache_lookup: bool = False,
    skip_cache_write: bool = False,
    knowledge_scope: dict | None = None,
    allowed_tools: list[str] | None = None,
    mcp_servers: list[str] | None = None,
) -> dict[str, Any]:
    """运行聊天主链路，并允许外部注入租户级运行时依赖。"""
    initial_state: ChatWorkflowState = {
        "question": question,
        "images": list(images or []),
        "cache_key": cache_key or question,
        "phone": phone,
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "session_id": session_id,
        "request_id": request_id,
        "cache_hit": False,
        "cached_answer": "",
        "cached_knowledge_hits": [],
        "cached_retrieval_trace": {},
        "blocked": False,
        "block_message": "",
        "guardrail_events": [],
        "original_question": question,
        "rewritten_question": question,
        "rewrite_applied": False,
        "rewrite_notes": [],
        "matched_entities": [],
        "query_intent": "knowledge",
        "answer_strategy": "knowledge_rag",
        "retrieval_judge": {},
        "retrieval_attempts": 0,
        "rag_results": [],
        "knowledge_context": "",
        "rag_runtime": rag_runtime or rag_engine,
        "app_loader": app_loader,
        "model_loader": model_loader,
        "prompt_loader": prompt_loader,
        "llm_context": llm_context or {},
        "knowledge_scope": knowledge_scope or {},
        "allowed_tools": allowed_tools or [],
        "mcp_servers": mcp_servers or [],
        "log_chat": log_chat,
        "skip_cache_lookup": skip_cache_lookup,
        "skip_cache_write": skip_cache_write or bool(images),
        "phase_timings": {},
        "answer_events": [],
        "answer_text": "",
        "selected_model": "",
        "llm_error_message": "",
    }
    if _compiled_graph is not None:
        result = await _compiled_graph.ainvoke(initial_state)
        result["orchestration_backend"] = "langgraph"
        return result
    result = _run_fallback(initial_state)
    result["orchestration_backend"] = "fallback"
    return result
