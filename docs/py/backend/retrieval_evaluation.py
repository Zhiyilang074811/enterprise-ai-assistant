"""检索与 RAG 质量评测。

在保留 hit@k 检索指标的基础上，复用真实聊天工作流生成答案，
并在依赖可用时补充 Ragas Faithfulness / Response Relevancy。
"""
from __future__ import annotations

import math
from typing import Any

from backend.chat_workflow import run_chat_workflow_with_runtime
from backend.embeddings import get_embedding_backend


class _ForcedBackendRuntime:
    """给评测链路强制指定检索后端，避免工作流内部把配置绕回默认值。"""

    def __init__(self, runtime: Any, forced_backend: str):
        self._runtime = runtime
        self._forced_backend = forced_backend

    def search(self, query: str, top_k: int = 5, backend_override: str | None = None) -> list[dict]:
        return self._runtime.search(query, top_k=top_k, backend_override=self._forced_backend)

    def get_stats(self) -> dict[str, Any]:
        stats = dict(self._runtime.get_stats() or {})
        stats["retrieval_backend"] = self._forced_backend
        return stats

    def _retrieval_cfg(self) -> dict[str, Any]:
        return self._runtime._retrieval_cfg()


def _normalize_case(case: dict[str, Any]) -> dict[str, Any]:
    """清洗评测题，保证字段稳定。"""
    reference_answer = (
        case.get("reference_answer")
        or case.get("ground_truth")
        or case.get("expected_answer")
        or case.get("reference")
        or ""
    )
    return {
        "question": str(case.get("question") or "").strip(),
        "expected_keywords": [str(item).strip() for item in (case.get("expected_keywords") or []) if str(item).strip()],
        "expected_tier": str(case.get("expected_tier") or "").strip(),
        "expected_source": str(case.get("expected_source") or "").strip(),
        "reference_answer": str(reference_answer or "").strip(),
    }


def _is_hit(result: dict[str, Any], case: dict[str, Any]) -> bool:
    """判断单条召回结果是否命中预期。"""
    source_text = " ".join(
        [
            str(result.get("source") or ""),
            str(result.get("title") or ""),
            str(result.get("content") or ""),
            str(result.get("snippet") or ""),
        ]
    ).lower()
    expected_keywords = [item.lower() for item in case.get("expected_keywords") or []]
    expected_tier = str(case.get("expected_tier") or "").strip().lower()
    expected_source = str(case.get("expected_source") or "").strip().lower()

    keyword_hit = True if not expected_keywords else any(keyword in source_text for keyword in expected_keywords)
    tier_hit = True if not expected_tier else str(result.get("tier") or "").strip().lower() == expected_tier
    source_hit = True if not expected_source else expected_source in source_text
    return keyword_hit and tier_hit and source_hit


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(float(a) * float(b) for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(float(a) * float(a) for a in vec_a))
    norm_b = math.sqrt(sum(float(b) * float(b) for b in vec_b))
    if norm_a <= 0 or norm_b <= 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _answer_relevance_proxy(question: str, answer: str, retrieval_config: dict[str, Any] | None) -> float:
    """没有 Ragas embeddings 时，退化为本地 embedding 相似度代理指标。"""
    if not question.strip() or not answer.strip():
        return 0.0
    try:
        backend = get_embedding_backend(retrieval_config)
        return round(_cosine_similarity(backend.embed(question), backend.embed(answer)), 4)
    except Exception:
        return 0.0


def _build_ragas_llm(model_settings: dict[str, Any] | None) -> tuple[Any | None, str]:
    try:
        from langchain_openai import ChatOpenAI
        from ragas.llms import LangchainLLMWrapper
    except Exception:
        return None, "missing_ragas_or_langchain_openai"

    settings = model_settings or {}
    model = str(settings.get("model_primary") or settings.get("model_fallback") or "").strip()
    base_url = str(settings.get("base_url") or "").strip()
    api_key = next((str(item).strip() for item in (settings.get("api_keys") or []) if str(item).strip()), "")
    if not model or not base_url or not api_key:
        return None, "missing_model_settings"
    try:
        client = ChatOpenAI(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=0,
        )
        return LangchainLLMWrapper(client), "ok"
    except Exception as exc:
        return None, f"llm_init_failed:{exc}"


def _build_ragas_embeddings(retrieval_config: dict[str, Any] | None) -> tuple[Any | None, str]:
    try:
        from langchain_openai import OpenAIEmbeddings
        from ragas.embeddings import LangchainEmbeddingsWrapper
    except Exception:
        return None, "missing_ragas_or_langchain_openai"

    config = retrieval_config or {}
    embedding_cfg = dict(config.get("embedding") or {})
    provider = str(embedding_cfg.get("provider") or "").strip().lower()
    model = str(embedding_cfg.get("model") or "").strip()
    base_url = str(embedding_cfg.get("base_url") or "").strip()
    api_key = str(embedding_cfg.get("api_key") or "").strip()
    if provider not in {"openai_compatible", "openai-compatible", "siliconcloud", "silicon_flow", "siliconflow"}:
        return None, "embedding_provider_not_supported"
    if not model or not base_url or not api_key:
        return None, "missing_embedding_settings"
    try:
        client = OpenAIEmbeddings(
            model=model,
            base_url=base_url,
            api_key=api_key,
        )
        return LangchainEmbeddingsWrapper(client), "ok"
    except Exception as exc:
        return None, f"embedding_init_failed:{exc}"


async def _apply_ragas_metrics(
    details: list[dict[str, Any]],
    *,
    model_settings: dict[str, Any] | None,
    retrieval_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """在依赖和配置都可用时，对每题补充 Ragas 指标。"""
    if not details:
        return {
            "provider": "builtin",
            "status": "skipped",
            "reason": "no_cases",
            "judged_cases": 0,
            "faithfulness_cases": 0,
            "answer_relevance_cases": 0,
        }
    try:
        from ragas import SingleTurnSample
        from ragas.metrics import Faithfulness, ResponseRelevancy
    except Exception:
        return {
            "provider": "builtin",
            "status": "skipped",
            "reason": "ragas_not_installed",
            "judged_cases": 0,
            "faithfulness_cases": 0,
            "answer_relevance_cases": 0,
        }

    ragas_llm, llm_status = _build_ragas_llm(model_settings)
    if ragas_llm is None:
        return {
            "provider": "builtin",
            "status": "skipped",
            "reason": llm_status,
            "judged_cases": 0,
            "faithfulness_cases": 0,
            "answer_relevance_cases": 0,
        }
    ragas_embeddings, embedding_status = _build_ragas_embeddings(retrieval_config)

    try:
        faithfulness_metric = Faithfulness(llm=ragas_llm)
        relevance_metric = ResponseRelevancy(llm=ragas_llm, embeddings=ragas_embeddings) if ragas_embeddings else None
    except Exception as exc:
        return {
            "provider": "builtin",
            "status": "skipped",
            "reason": f"ragas_metric_init_failed:{exc}",
            "judged_cases": 0,
            "faithfulness_cases": 0,
            "answer_relevance_cases": 0,
        }
    faithfulness_scores: list[float] = []
    relevance_scores: list[float] = []

    for item in details:
        answer = str(item.get("generated_answer") or "").strip()
        contexts = [str(chunk).strip() for chunk in (item.get("retrieved_contexts") or []) if str(chunk).strip()]
        if not answer or not contexts:
            continue
        sample = SingleTurnSample(
            user_input=str(item.get("question") or ""),
            response=answer,
            retrieved_contexts=contexts,
            reference=str(item.get("reference_answer") or ""),
        )
        try:
            faithfulness = float(await faithfulness_metric.single_turn_ascore(sample))
            item["faithfulness"] = round(faithfulness, 4)
            faithfulness_scores.append(faithfulness)
        except Exception as exc:
            item["faithfulness_error"] = str(exc)
        if relevance_metric is not None:
            try:
                answer_relevance = float(await relevance_metric.single_turn_ascore(sample))
                item["answer_relevance"] = round(answer_relevance, 4)
                relevance_scores.append(answer_relevance)
            except Exception as exc:
                item["answer_relevance_error"] = str(exc)

    return {
        "provider": "ragas",
        "status": "enabled",
        "reason": "",
        "llm_status": llm_status,
        "embedding_status": embedding_status,
        "judged_cases": len(faithfulness_scores) or len(relevance_scores),
        "faithfulness_cases": len(faithfulness_scores),
        "answer_relevance_cases": len(relevance_scores),
        "avg_faithfulness": round(sum(faithfulness_scores) / len(faithfulness_scores), 4) if faithfulness_scores else None,
        "avg_answer_relevance": round(sum(relevance_scores) / len(relevance_scores), 4) if relevance_scores else None,
    }


async def run_retrieval_evaluation(
    *,
    rag_runtime: Any,
    cases: list[dict[str, Any]],
    retrieval_config: dict[str, Any] | None = None,
    backend_override: str | None = None,
    tenant_id: str = "default",
    app_loader=None,
    model_loader=None,
    prompt_loader=None,
    llm_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """运行一轮检索评测，并在可用时补充 RAG 质量指标。"""
    normalized_cases = [_normalize_case(case) for case in cases]
    valid_cases = [case for case in normalized_cases if case["question"]]
    details: list[dict[str, Any]] = []
    hit_at_1 = 0
    hit_at_3 = 0
    hit_at_5 = 0
    top_scores: list[float] = []
    runtime = _ForcedBackendRuntime(rag_runtime, backend_override) if backend_override else rag_runtime

    for index, case in enumerate(valid_cases):
        workflow_result = await run_chat_workflow_with_runtime(
            question=case["question"],
            phone="rag_eval",
            cache_key=f"rag_eval::{tenant_id}::{index}",
            tenant_id=tenant_id,
            request_id=f"rag_eval_{tenant_id}_{index}",
            rag_runtime=runtime,
            app_loader=app_loader,
            model_loader=model_loader,
            prompt_loader=prompt_loader,
            llm_context=llm_context or {},
            log_chat=False,
            skip_cache_lookup=True,
            skip_cache_write=True,
        )
        results = list(workflow_result.get("rag_results") or [])
        top_scores.append(float(results[0].get("score") or 0) if results else 0.0)
        top1 = results[:1]
        top3 = results[:3]
        top5 = results[:5]
        top1_hit = any(_is_hit(item, case) for item in top1)
        top3_hit = any(_is_hit(item, case) for item in top3)
        top5_hit = any(_is_hit(item, case) for item in top5)
        if top1_hit:
            hit_at_1 += 1
        if top3_hit:
            hit_at_3 += 1
        if top5_hit:
            hit_at_5 += 1

        generated_answer = str(workflow_result.get("answer_text") or "").strip()
        detail = {
            "question": case["question"],
            "expected_keywords": case["expected_keywords"],
            "expected_tier": case["expected_tier"],
            "expected_source": case["expected_source"],
            "reference_answer": case["reference_answer"],
            "top1_hit": top1_hit,
            "top3_hit": top3_hit,
            "top5_hit": top5_hit,
            "top_score": round(float(results[0].get("score") or 0), 4) if results else 0.0,
            "generated_answer": generated_answer,
            "selected_model": str(workflow_result.get("selected_model") or ""),
            "retrieval_backend": str(workflow_result.get("retrieval_backend") or ""),
            "rewrite_applied": bool(workflow_result.get("rewrite_applied")),
            "retrieval_attempts": int(workflow_result.get("retrieval_attempts") or 0),
            "llm_error_message": str(workflow_result.get("llm_error_message") or ""),
            "retrieved_contexts": [str(item.get("content") or "").strip() for item in results if str(item.get("content") or "").strip()],
            "results": [
                {
                    "source": item.get("source", ""),
                    "tier": item.get("tier", ""),
                    "score": round(float(item.get("score") or 0), 4),
                    "backend": item.get("backend", ""),
                }
                for item in results
            ],
        }
        detail["answer_relevance_proxy"] = _answer_relevance_proxy(case["question"], generated_answer, retrieval_config)
        details.append(detail)

    model_settings = model_loader() if callable(model_loader) else {}
    quality_summary = await _apply_ragas_metrics(
        details,
        model_settings=model_settings,
        retrieval_config=retrieval_config,
    )

    total = len(valid_cases)
    avg_top_score = round(sum(top_scores) / total, 4) if total else 0.0
    avg_answer_relevance_proxy = round(
        sum(float(item.get("answer_relevance_proxy") or 0) for item in details) / total,
        4,
    ) if total else 0.0
    avg_faithfulness = quality_summary.get("avg_faithfulness")
    avg_answer_relevance = quality_summary.get("avg_answer_relevance")
    return {
        "total_questions": total,
        "hit_at_1": hit_at_1,
        "hit_at_3": hit_at_3,
        "hit_at_5": hit_at_5,
        "avg_top_score": avg_top_score,
        "avg_answer_relevance_proxy": avg_answer_relevance_proxy,
        "avg_faithfulness": avg_faithfulness,
        "avg_answer_relevance": avg_answer_relevance,
        "detail": details,
        "config_snapshot": {
            "backend_override": backend_override or "",
            "retrieval_config": retrieval_config or {},
            "quality_summary": quality_summary,
        },
    }
