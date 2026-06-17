"""租户工作流执行引擎。"""
from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import shlex
import re
import shutil
import smtplib
import ssl
import textwrap
import time
from types import SimpleNamespace
from copy import deepcopy
from email.mime.text import MIMEText
from typing import Any, Callable, TypedDict

import aiohttp

from backend.concurrency import BusyError, acquire_llm_slot, acquire_workflow_io_slot
from backend.llm_service import build_provider_route
from backend.model_config import load_model_config
from backend.knowledge_assets import annotate_retrieval_results_with_scope
from backend.rag import build_runtime_rag_engine
from backend.retrieval_orchestration import (
    build_retry_stages,
    choose_retrieval_route,
    get_retry_plan,
    judge_retrieval_quality,
    rewrite_query,
)
from backend.retrieval_config import load_retrieval_config
from backend.tenant_config import get_tenant_knowledge_dir, load_tenant_app_config, load_tenant_system_prompt
from backend.tool_config import load_tool_config
from backend.workflow_config import load_workflow_config

try:
    from backend.hospital_mock import is_mock_hospital_mcp_url, mock_hospital_mcp_result
except Exception:  # pragma: no cover
    def is_mock_hospital_mcp_url(url: str) -> bool:
        return False

    def mock_hospital_mcp_result(tool_name: str, payload: object) -> dict:
        raise WorkflowRuntimeError("本地调试桥接未启用")

try:
    from langgraph.graph import END, START, StateGraph

    LANGGRAPH_AVAILABLE = True
except Exception:  # pragma: no cover
    END = "__end__"
    START = "__start__"
    StateGraph = None
    LANGGRAPH_AVAILABLE = False


class WorkflowRuntimeError(RuntimeError):
    """工作流运行错误。"""


class WorkflowGraphState(TypedDict, total=False):
    input: dict[str, Any]
    nodes: dict[str, Any]
    last_result: dict[str, Any]
    notifications: list[dict[str, Any]]
    forms: dict[str, Any]
    workflow: dict[str, Any]
    logs: list[dict[str, Any]]
    next_node_id: str
    entry_node_id: str
    stop_before: list[str]
    stopped_at: str


_verify_ssl = os.environ.get("VERIFY_SSL", "0").strip()
if _verify_ssl == "1":
    try:
        import certifi

        WORKFLOW_SSL_CTX = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        WORKFLOW_SSL_CTX = ssl.create_default_context()
else:
    WORKFLOW_SSL_CTX = False


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _safe_json_loads(value: object, fallback):
    if isinstance(value, (dict, list)):
        return deepcopy(value)
    text = str(value or "").strip()
    if not text:
        return deepcopy(fallback)
    try:
        parsed = json.loads(text)
    except Exception:
        return deepcopy(fallback)
    return parsed


def _extract_json_text(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    direct = _safe_json_loads(text, None)
    if isinstance(direct, (dict, list)):
        return text
    block_match = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.S | re.I)
    if block_match:
        candidate = str(block_match.group(1) or "").strip()
        parsed = _safe_json_loads(candidate, None)
        if isinstance(parsed, (dict, list)):
            return candidate
    brace_match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
    if brace_match:
        candidate = str(brace_match.group(1) or "").strip()
        parsed = _safe_json_loads(candidate, None)
        if isinstance(parsed, (dict, list)):
            return candidate
    return ""


def _normalize_ai_structured_output(value: object) -> dict:
    candidate = _extract_json_text(value)
    parsed = _safe_json_loads(candidate, None) if candidate else None
    if not isinstance(parsed, dict):
        return {}
    render_payload = parsed.get("render_payload") if isinstance(parsed.get("render_payload"), dict) else {}
    if not render_payload and any(key in parsed for key in ("type", "title", "cards", "tables", "charts", "sections", "sections_extra")):
        render_payload = deepcopy(parsed)
    answer_text = ""
    for key in ("answer_text", "answer", "text", "markdown", "content", "summary"):
        current = parsed.get(key)
        if isinstance(current, str) and current.strip():
            answer_text = current.strip()
            break
    return {
        "json_text": candidate,
        "parsed": parsed,
        "answer_text": answer_text,
        "render_payload": render_payload if isinstance(render_payload, dict) else {},
    }


def _to_number(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _contains(container: object, needle: object) -> bool:
    if container is None:
        return False
    return str(needle) in str(container)


def _dot_get(data: object, path: str, default: object = "") -> object:
    if not path:
        return data
    current = data
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part, default)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else default
        else:
            current = getattr(current, part, default)
        if current is default:
            break
    return current


_TPL_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


def _render_template(value: object, context: dict) -> object:
    if isinstance(value, dict):
        return {k: _render_template(v, context) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(item, context) for item in value]
    text = str(value or "")
    if "{{" not in text:
        return text
    return _TPL_RE.sub(lambda m: str(_dot_get(context, m.group(1), "")), text)


def _prepare_condition(expr: str) -> str:
    result = str(expr or "").strip()
    result = result.replace("&&", " and ").replace("||", " or ")
    result = result.replace("===", "==").replace("!==", "!=")
    result = re.sub(r"(\S+)\.includes\(", r"contains(\1, ", result)
    return result


def _to_attr_object(value: object):
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _to_attr_object(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_to_attr_object(item) for item in value]
    return value


def _eval_condition(expr: str, context: dict) -> bool:
    safe_expr = _prepare_condition(expr)
    if not safe_expr:
        return False
    attr_context = {key: _to_attr_object(value) for key, value in context.items()}
    locals_map = {
        "contains": _contains,
        "len": len,
        "int": int,
        "float": float,
        "str": str,
        "bool": bool,
        "math": math,
        **attr_context,
    }
    return bool(eval(safe_expr, {"__builtins__": {}}, locals_map))


def _parse_bool(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on", "是"}


def _node_label(node: dict) -> str:
    data = node.get("data") if isinstance(node.get("data"), dict) else {}
    return str(data.get("label") or node.get("type") or node.get("id") or "node")


def _guess_agent_name(node_data: dict) -> str:
    for raw in [
        node_data.get("assistantName"),
        node_data.get("role"),
        node_data.get("label"),
        node_data.get("description"),
        node_data.get("prompt"),
    ]:
        text = str(raw or "").strip()
        if not text:
            continue
        match = re.search(r"“([^”]{2,30})”", text)
        if match:
            return match.group(1)
        for suffix in ("答复", "流程", "检索", "规则核验", "生成", "节点"):
            text = text.replace(suffix, "")
        text = text.strip(" ：:-")
        if "助手" in text:
            return text
    return "医院助手"


def _clean_text_lines(text: object) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip().lstrip("-").strip()
        if len(line) < 4:
            continue
        key = line.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(line)
    return result


def _summarize_knowledge_context(context: dict) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for node_result in (context.get("nodes") or {}).values():
        if not isinstance(node_result, dict):
            continue
        for hit in node_result.get("hits") or []:
            if not isinstance(hit, dict):
                continue
            for line in _clean_text_lines(hit.get("content")):
                if line.lower() in seen:
                    continue
                seen.add(line.lower())
                lines.append(line)
                if len(lines) >= 6:
                    return lines
        for line in _clean_text_lines(node_result.get("knowledge_text")):
            if line.lower() in seen:
                continue
            seen.add(line.lower())
            lines.append(line)
            if len(lines) >= 6:
                return lines
    return lines


def _summarize_mcp_context(context: dict) -> tuple[list[str], list[str]]:
    highlights: list[str] = []
    manual_flags: list[str] = []
    seen: set[str] = set()
    for node_result in (context.get("nodes") or {}).values():
        if not isinstance(node_result, dict):
            continue
        payload = node_result.get("result")
        if not isinstance(payload, dict):
            continue
        payload_body = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        if not isinstance(payload_body, dict):
            continue
        for key in ("summary", "triage_advice", "eligibility", "manual_review"):
            value = str(payload_body.get(key) or "").strip()
            if value and value.lower() not in seen:
                seen.add(value.lower())
                highlights.append(value)
        for key in ("coverage_scope", "decision_points", "contraindications", "required_materials", "highlights"):
            values = payload_body.get(key)
            if not isinstance(values, list):
                continue
            for item in values:
                value = str(item or "").strip()
                if value and value.lower() not in seen:
                    seen.add(value.lower())
                    highlights.append(value)
        manual_review = str(payload_body.get("manual_review") or "").strip()
        if manual_review:
            manual_flags.append(manual_review)
    return highlights[:6], manual_flags[:3]


def _build_ai_fallback_text(*, node_data: dict, context: dict, reason: str) -> str:
    agent_name = _guess_agent_name(node_data)
    question = str(_dot_get(context, "input.text", "") or "").strip()
    knowledge_lines = _summarize_knowledge_context(context)
    mcp_lines, manual_flags = _summarize_mcp_context(context)
    combined = []
    for item in knowledge_lines + mcp_lines:
        clean = str(item or "").strip()
        if clean and clean not in combined:
            combined.append(clean)
    if not combined:
        combined.append("当前已进入医院业务流程，但本次命中的知识依据较少，建议转人工窗口或专科门诊进一步确认。")

    intro = f"{agent_name}已根据当前问题进入对应业务流程。"
    if any(word in question for word in ("急诊", "胸痛", "胸闷", "呼吸困难", "意识", "高热", "出血")):
        intro = f"{agent_name}判断这类情况需要优先关注风险分层，若症状正在加重请先按急诊流程处理。"
    elif any(word in question for word in ("医保", "报销", "备案", "复诊", "预约", "挂号")):
        intro = f"{agent_name}判断这类问题以院内预约和医保规则为主，先按流程准备资料再办理。"
    elif any(word in question for word in ("报告", "血常规", "CT", "MRI", "复查")):
        intro = f"{agent_name}判断这是检查结果解释与复查提醒场景，先看异常项，再决定复查和就诊动作。"

    lines = [intro, "", "建议这样处理："]
    for index, item in enumerate(combined[:4], start=1):
        lines.append(f"{index}. {item}")
    if manual_flags:
        lines.extend(["", "请转人工："])
        for item in manual_flags[:2]:
            lines.append(f"- {item}")
    lines.extend(
        [
            "",
            "说明：本次答复已走医院知识库和流程节点兜底生成；如现场执行口径有调整，以医院窗口和临床当班人员最终说明为准。",
            f"流程状态：AI 节点已自动降级处理（{reason}）。",
        ]
    )
    return "\n".join(lines)


async def _call_llm(*, prompt: str, model_settings: dict, node_data: dict, images: list[dict] | None = None) -> dict:
    workflow_route = []
    chosen_model = str(node_data.get("model") or "").strip()
    if chosen_model and chosen_model != "__default__":
        workflow_route.append(chosen_model)
    provider_routes = build_provider_route(
        model_settings=model_settings,
        workflow_route=workflow_route,
        default_base_url=str(model_settings.get("base_url") or ""),
    )
    if not provider_routes:
        raise WorkflowRuntimeError("当前租户没有可用模型供应商")
    temperature = _to_number(node_data.get("temperature"), 0.7)
    max_tokens = int(_to_number(node_data.get("max_tokens"), 1200))
    normalized_images = [
        {
            "data_url": str(item.get("data_url") or "").strip(),
            "mime_type": str(item.get("mime_type") or "image/jpeg").strip() or "image/jpeg",
            "name": str(item.get("name") or "image").strip() or "image",
        }
        for item in (images or [])
        if isinstance(item, dict) and str(item.get("data_url") or "").strip().startswith("data:image/")
    ]
    if normalized_images:
        provider_routes = [provider for provider in provider_routes if bool(provider.get("supports_image"))]
        if not provider_routes:
            raise WorkflowRuntimeError("当前智能体绑定的模型仅支持文本输入，请切换到支持图文的模型后再上传图片。")
    last_error = "模型请求失败"
    for provider in provider_routes:
        for model in provider.get("model_route") or []:
            api_keys = list(provider.get("api_keys") or [])
            for api_key in api_keys[:3]:
                try:
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "你是企业自动化流程中的智能处理节点，请只输出对后续节点有用的结果。"},
                            {"role": "user", "content": _build_workflow_user_content(prompt, normalized_images)},
                        ],
                        "stream": False,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }
                    headers = {
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    }
                    async with acquire_llm_slot():
                        connector = aiohttp.TCPConnector(ssl=WORKFLOW_SSL_CTX)
                        async with aiohttp.ClientSession(connector=connector) as session:
                            async with session.post(
                                f"{str(provider.get('base_url') or '').rstrip('/')}/chat/completions",
                                json=payload,
                                headers=headers,
                                timeout=aiohttp.ClientTimeout(total=120),
                            ) as resp:
                                text = await resp.text()
                                if resp.status >= 400:
                                    last_error = f"模型接口异常: HTTP {resp.status}"
                                    continue
                                data = json.loads(text)
                                choices = data.get("choices")
                                first_choice = choices[0] if isinstance(choices, list) and choices else {}
                                message = first_choice.get("message", {}) if isinstance(first_choice, dict) else {}
                                content = ""
                                if isinstance(message, dict):
                                    content = str(message.get("content", "") or "").strip()
                                if not content and isinstance(first_choice, dict):
                                    content = str(first_choice.get("text", "") or "").strip()
                                if not content:
                                    last_error = str(data.get("error", {}).get("message") or data.get("message") or "模型返回空结果")
                                    continue
                                return {
                                    "provider_id": provider.get("provider_id", ""),
                                    "provider_label": provider.get("provider_label", ""),
                                    "model": model,
                                    "text": content,
                                    "raw": data,
                                }
                except BusyError:
                    last_error = "模型通道繁忙，请稍后再试"
                except Exception as exc:
                    last_error = str(exc)
                    continue
    raise WorkflowRuntimeError(last_error)


def _build_workflow_user_content(prompt: str, images: list[dict]) -> str | list[dict]:
    clean_prompt = str(prompt or "").strip()
    if not images:
        return clean_prompt
    content: list[dict] = []
    if clean_prompt:
        content.append({"type": "text", "text": clean_prompt})
    for item in images:
        data_url = str(item.get("data_url") or "").strip()
        if data_url:
            content.append({"type": "image_url", "image_url": {"url": data_url}})
    return content or clean_prompt


def _filter_knowledge_hits(results: list[dict], node_data: dict) -> list[dict]:
    threshold = _to_number(node_data.get("threshold"), 0.0)
    knowledge_base = str(node_data.get("knowledgeBase") or "").strip()
    legacy_names = {"默认知识库", "全部知识库", "L1 基础库", "L2 增量库", "L3 热库"}
    filtered = []
    for item in results:
        score = _to_number(item.get("score"), 0.0)
        if score < threshold:
            continue
        if knowledge_base and knowledge_base not in legacy_names:
            item_library = str(item.get("library_id") or "").strip()
            item_library_name = str(item.get("library_name") or "").strip()
            if knowledge_base not in {item_library, item_library_name}:
                continue
        filtered.append(item)
    return filtered


def _knowledge_scope_from_node(node_data: dict) -> dict:
    knowledge_base = str(node_data.get("knowledgeBase") or "").strip()
    if knowledge_base in {"默认知识库", "全部知识库", "L1 基础库", "L2 增量库", "L3 热库"}:
        return {}
    if knowledge_base:
        return {"libraries": [knowledge_base]}
    return {}


def _run_workflow_knowledge_search(
    *,
    tenant_id: str,
    query: str,
    top_k: int,
    node_data: dict,
    rag_runtime,
    retrieval_config: dict,
) -> dict:
    """工作流知识节点复用统一检索编排，只负责控制触发时机和局部参数。"""
    clean_query = str(query or "").strip()
    requested_top_k = max(1, int(top_k or 1))
    knowledge_scope = _knowledge_scope_from_node(node_data)
    preferred_backend = str(rag_runtime.get_stats().get("retrieval_backend") or retrieval_config.get("backend") or "hybrid")
    route = choose_retrieval_route(
        clean_query,
        retrieval_config,
        preferred_backend=preferred_backend,
    )
    retry_plan = get_retry_plan(retrieval_config)
    scoped_top_k = max(requested_top_k, 12) if knowledge_scope else requested_top_k
    backend = str(route.get("backend") or preferred_backend or "hybrid")

    raw_results = annotate_retrieval_results_with_scope(
        tenant_id=tenant_id,
        results=rag_runtime.search(
            query=clean_query,
            top_k=scoped_top_k,
            backend_override=backend,
            knowledge_scope=knowledge_scope,
        ),
        knowledge_scope=knowledge_scope,
    )[:requested_top_k]
    filtered_results = _filter_knowledge_hits(raw_results, node_data)[:requested_top_k]
    judge = judge_retrieval_quality(filtered_results, retrieval_config)
    attempts = 1
    strategy_trace: list[dict[str, Any]] = [
        {
            "attempt": 1,
            "backend": backend,
            "top_k": requested_top_k,
            "profile": route.get("profile"),
            "judge": judge,
            "query": clean_query,
            "strategy": route.get("strategy"),
        }
    ]
    final_query = clean_query

    if not judge.get("ok") and retry_plan.get("enabled"):
        max_attempts = int(retry_plan.get("max_attempts", 1) or 1)
        for stage in build_retry_stages(
            clean_query,
            retrieval_config,
            preferred_backend=backend,
        ):
            if attempts >= max_attempts or judge.get("ok"):
                break
            rewritten = rewrite_query(
                clean_query,
                retrieval_config,
                profile=str(route.get("profile") or ""),
                attempt=int(stage.get("attempt") or attempts + 1),
                mode=str(stage.get("rewrite_mode") or "normal"),
            )
            stage_query = str(rewritten.get("rewritten") or clean_query).strip() or clean_query
            stage_backend = str(stage.get("backend") or backend or preferred_backend or "hybrid")
            stage_top_k = max(1, int(stage.get("top_k") or retry_plan.get("fallback_top_k", requested_top_k) or requested_top_k))
            stage_requested_top_k = max(stage_top_k, 12) if knowledge_scope else stage_top_k
            stage_raw_results = annotate_retrieval_results_with_scope(
                tenant_id=tenant_id,
                results=rag_runtime.search(
                    query=stage_query,
                    top_k=stage_requested_top_k,
                    backend_override=stage_backend,
                    knowledge_scope=knowledge_scope,
                ),
                knowledge_scope=knowledge_scope,
            )[:stage_top_k]
            stage_filtered_results = _filter_knowledge_hits(stage_raw_results, node_data)[:stage_top_k]
            judge = judge_retrieval_quality(stage_filtered_results, retrieval_config)
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
            raw_results = stage_raw_results
            filtered_results = stage_filtered_results
            final_query = stage_query
            if judge.get("ok"):
                break

    retrieval_backend = str(filtered_results[0].get("backend") or raw_results[0].get("backend") or backend) if (filtered_results or raw_results) else backend
    return {
        "hits": filtered_results,
        "raw_hits": raw_results,
        "knowledge_scope": knowledge_scope,
        "retrieval_backend": retrieval_backend,
        "retrieval_route": route,
        "retrieval_attempts": attempts,
        "retrieval_judge": judge,
        "retrieval_strategy_trace": strategy_trace,
        "query_profile": str(route.get("profile") or ""),
        "query": final_query,
        "requested_top_k": requested_top_k,
    }


def _outgoing_map(connections: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for item in connections:
        result.setdefault(str(item.get("from") or ""), []).append(item)
    return result


def _incoming_map(connections: list[dict]) -> dict[str, list[dict]]:
    result: dict[str, list[dict]] = {}
    for item in connections:
        result.setdefault(str(item.get("to") or ""), []).append(item)
    return result


def _find_start_node(nodes: list[dict]) -> dict:
    start = next((node for node in nodes if node.get("type") == "start"), None)
    if start:
        return start
    if nodes:
        return nodes[0]
    raise WorkflowRuntimeError("工作流没有节点")


def _find_merge_node(start_ids: list[str], outgoing: dict[str, list[dict]]) -> str | None:
    reachable_sets: list[set[str]] = []
    for start_id in start_ids:
        seen: set[str] = set()
        queue = [start_id]
        while queue:
            current = queue.pop(0)
            for conn in outgoing.get(current, []):
                nxt = str(conn.get("to") or "")
                if nxt and nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        reachable_sets.append(seen)
    if not reachable_sets:
        return None
    common = set.intersection(*reachable_sets) if len(reachable_sets) > 1 else reachable_sets[0]
    if not common:
        return None
    for node_id in common:
        return node_id
    return None


def _merge_runtime_state(
    parent_state: WorkflowGraphState,
    branch_state: WorkflowGraphState,
    *,
    base_log_count: int = 0,
    base_notification_count: int = 0,
    base_node_keys: set[str] | None = None,
    base_form_keys: set[str] | None = None,
) -> None:
    branch_nodes = branch_state.get("nodes") or {}
    for key, value in branch_nodes.items():
        if not base_node_keys or key not in base_node_keys or parent_state["nodes"].get(key) != value:
            parent_state["nodes"][key] = deepcopy(value)
    branch_forms = branch_state.get("forms") or {}
    for key, value in branch_forms.items():
        if not base_form_keys or key not in base_form_keys or parent_state["forms"].get(key) != value:
            parent_state["forms"][key] = deepcopy(value)
    branch_notifications = list(branch_state.get("notifications") or [])
    if base_notification_count <= len(branch_notifications):
        parent_state["notifications"].extend(deepcopy(branch_notifications[base_notification_count:]))
    else:
        parent_state["notifications"] = deepcopy(branch_notifications)
    parent_state["last_result"] = deepcopy(branch_state.get("last_result") or {})
    branch_logs = list(branch_state.get("logs") or [])
    if base_log_count <= len(branch_logs):
        parent_state["logs"].extend(deepcopy(branch_logs[base_log_count:]))
    else:
        parent_state["logs"][:] = deepcopy(branch_logs)
    parent_state["stopped_at"] = str(branch_state.get("stopped_at") or "")
    parent_state["next_node_id"] = str(branch_state.get("next_node_id") or "")


async def _send_email(tool_cfg: dict, *, to_email: str, subject: str, content: str) -> dict:
    email_cfg = tool_cfg.get("email") if isinstance(tool_cfg.get("email"), dict) else {}
    if not email_cfg.get("enabled"):
        return {"ok": False, "msg": "邮件工具未启用"}
    host = str(email_cfg.get("smtp_host") or "").strip()
    port = int(email_cfg.get("smtp_port") or 465)
    username = str(email_cfg.get("username") or "").strip()
    password = str(email_cfg.get("password") or "").strip()
    from_email = str(email_cfg.get("from_email") or username).strip()
    from_name = str(email_cfg.get("from_name") or "企业知识助手").strip()
    if not host or not from_email:
        return {"ok": False, "msg": "邮件配置不完整"}
    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email

    def _send() -> None:
        if email_cfg.get("use_ssl", True):
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
        with server:
            if email_cfg.get("use_tls"):
                server.starttls(context=ssl.create_default_context())
            if username:
                server.login(username, password)
            server.send_message(msg)

    try:
        await asyncio.to_thread(_send)
        return {"ok": True, "msg": "邮件已发送"}
    except Exception as exc:
        return {"ok": False, "msg": str(exc)}


async def _call_mcp_server(
    tool_cfg: dict,
    *,
    node_data: dict,
    context: dict,
    tenant_id: str,
    tenant_name: str,
) -> dict:
    mcp_cfg = tool_cfg.get("mcp") if isinstance(tool_cfg.get("mcp"), dict) else {}
    if not mcp_cfg.get("enabled"):
        raise WorkflowRuntimeError("MCP 工具未启用")
    server_id = str(_render_template(node_data.get("serverId") or "", context)).strip()
    tool_name = str(_render_template(node_data.get("toolName") or "", context)).strip()
    payload_rendered = _render_template(node_data.get("payload") or "{}", context)
    payload_data = _safe_json_loads(payload_rendered, None)
    if not server_id:
        raise WorkflowRuntimeError("MCP 节点缺少服务 ID")
    if not tool_name:
        raise WorkflowRuntimeError("MCP 节点缺少工具名称")
    servers = mcp_cfg.get("servers") if isinstance(mcp_cfg.get("servers"), list) else []
    target = None
    for item in servers:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        current_id = str(item.get("server_id") or item.get("id") or "").strip()
        if current_id == server_id:
            target = item
            break
    if not target:
        raise WorkflowRuntimeError(f"MCP 服务不存在或未启用：{server_id}")
    transport = str(target.get("transport") or ("http" if (target.get("bridge_url") or target.get("url")) else "stdio")).strip().lower() or "http"
    bridge_url = str(target.get("bridge_url") or target.get("url") or "").strip()
    command = str(target.get("command") or "").strip()
    headers = {}
    if isinstance(target.get("headers"), dict):
        headers.update({str(k): str(v) for k, v in target.get("headers", {}).items()})
    auth_token = str(target.get("auth_token") or "").strip()
    if auth_token:
        headers.setdefault("Authorization", f"Bearer {auth_token}")
    headers.setdefault("Content-Type", "application/json")
    timeout_seconds = max(3, int(_to_number(mcp_cfg.get("request_timeout_seconds"), 30)))
    request_body = {
        "tool": tool_name,
        "input": payload_data if payload_data is not None else str(payload_rendered or ""),
        "context": {
            "tenant_id": tenant_id,
            "tenant_name": tenant_name,
            "input": deepcopy(context.get("input") or {}),
            "last": deepcopy(context.get("last") or {}),
        },
    }
    if transport == "http" and not bridge_url:
        raise WorkflowRuntimeError(f"MCP 服务未配置调用地址：{server_id}")
    if transport == "stdio" and not command:
        raise WorkflowRuntimeError(f"MCP 服务未配置启动命令：{server_id}")
    if transport == "http" and is_mock_hospital_mcp_url(bridge_url):
        mock_result = mock_hospital_mcp_result(tool_name, request_body["input"])
        return {
            "ok": True,
            "status": 200,
            "server_id": server_id,
            "server_label": str(target.get("label") or server_id),
            "tool": tool_name,
            "request": request_body,
            "body": {
                "ok": True,
                "message": "local mcp bridge",
                "result": mock_result,
            },
            "result": mock_result,
            "message": "local mcp bridge",
        }
    if transport == "stdio":
        args = [str(v) for v in (target.get("args") or []) if str(v).strip()]
        if not args and " " in command:
            command_parts = shlex.split(command)
            command = command_parts[0]
            args = command_parts[1:]
        env = os.environ.copy()
        if isinstance(target.get("env"), dict):
            env.update({str(k): str(v) for k, v in target.get("env", {}).items()})
        for key in (target.get("env_passthrough") or []):
            clean_key = str(key or "").strip()
            if clean_key and clean_key in os.environ:
                env[clean_key] = os.environ[clean_key]
        try:
            async with acquire_workflow_io_slot():
                proc = await asyncio.create_subprocess_exec(
                    command,
                    *args,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        proc.communicate(json.dumps(request_body, ensure_ascii=False).encode("utf-8")),
                        timeout=timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                    raise
                text = (stdout or b"").decode("utf-8", errors="replace").strip()
                parsed = _safe_json_loads(text, {})
                stderr_text = (stderr or b"").decode("utf-8", errors="replace").strip()
                ok = proc.returncode == 0 and not (isinstance(parsed, dict) and parsed.get("ok") is False)
                return {
                    "ok": ok,
                    "status": proc.returncode or 0,
                    "server_id": server_id,
                    "server_label": str(target.get("label") or server_id),
                    "tool": tool_name,
                    "request": request_body,
                    "body": parsed if parsed else text,
                    "result": parsed.get("result") if isinstance(parsed, dict) and "result" in parsed else (parsed if parsed else text),
                    "message": str(parsed.get("message") or stderr_text or "") if isinstance(parsed, dict) else stderr_text,
                    "stderr": stderr_text,
                }
        except BusyError:
            return {
                "ok": False,
                "status": 503,
                "server_id": server_id,
                "server_label": str(target.get("label") or server_id),
                "tool": tool_name,
                "request": request_body,
                "body": {"ok": False, "message": "工作流外部接口繁忙"},
                "result": None,
                "message": "工作流外部接口繁忙",
            }
        except asyncio.TimeoutError:
            return {
                "ok": False,
                "status": 504,
                "server_id": server_id,
                "server_label": str(target.get("label") or server_id),
                "tool": tool_name,
                "request": request_body,
                "body": {"ok": False, "message": "MCP STDIO 调用超时"},
                "result": None,
                "message": "MCP STDIO 调用超时",
            }
    try:
        async with acquire_workflow_io_slot():
            connector = aiohttp.TCPConnector(ssl=WORKFLOW_SSL_CTX)
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    bridge_url,
                    json=request_body,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_seconds),
                ) as resp:
                    text = await resp.text()
                    parsed = _safe_json_loads(text, {})
                    ok = resp.status < 400 and not (isinstance(parsed, dict) and parsed.get("ok") is False)
                    return {
                        "ok": ok,
                        "status": resp.status,
                        "server_id": server_id,
                        "server_label": str(target.get("label") or server_id),
                        "tool": tool_name,
                        "request": request_body,
                        "body": parsed if parsed else text,
                        "result": parsed.get("result") if isinstance(parsed, dict) and "result" in parsed else (parsed if parsed else text),
                        "message": str(parsed.get("message") or "") if isinstance(parsed, dict) else "",
                    }
    except BusyError:
        return {
            "ok": False,
            "status": 503,
            "server_id": server_id,
            "server_label": str(target.get("label") or server_id),
            "tool": tool_name,
            "request": request_body,
            "body": {"ok": False, "message": "工作流外部接口繁忙"},
            "result": None,
            "message": "工作流外部接口繁忙",
        }


async def _run_script(node_data: dict, runtime_context: dict) -> dict:
    script_type = str(node_data.get("scriptType") or "Python").strip()
    code = str(node_data.get("code") or "").strip()
    if not code:
        return {"ok": True, "stdout": "", "stderr": "", "result": None}
    timeout_seconds = max(1, int(_to_number(node_data.get("timeout"), 30)))
    input_payload = {
        "input": runtime_context.get("input") or {},
        "state": runtime_context.get("state") or {},
        "last_result": runtime_context.get("last_result") or {},
    }
    if script_type.lower() == "javascript":
        node_bin = shutil.which("node")
        if not node_bin:
            raise WorkflowRuntimeError("当前环境未安装 Node.js，无法执行 JavaScript 节点")
        wrapper = textwrap.dedent(
            """
            const input = JSON.parse(process.env.WF_INPUT || "{}");
            let result = null;
            let exports = {};
            let module = { exports };
            async function main(ctx) {
            %s
            }
            Promise.resolve(main(input)).then((value) => {
              const output = value === undefined ? (module.exports || exports || result) : value;
              process.stdout.write(JSON.stringify({ result: output }));
            }).catch((err) => {
              process.stderr.write(String(err && err.stack ? err.stack : err));
              process.exit(1);
            });
            """
        ).strip() % textwrap.indent(code, "  ")
        proc = await asyncio.create_subprocess_exec(
            node_bin,
            "-e",
            wrapper,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"WF_INPUT": json.dumps(input_payload, ensure_ascii=False)},
        )
    else:
        payload_literal = repr(input_payload)
        wrapper = (
            "import json\n"
            f"ctx = {payload_literal}\n"
            "result = None\n"
            f"{code}\n"
            "print(json.dumps({\"result\": result}, ensure_ascii=False))\n"
        ).strip()
        proc = await asyncio.create_subprocess_exec(
            shutil.which("python3") or "python3",
            "-c",
            wrapper,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError as exc:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise WorkflowRuntimeError(f"脚本执行超时（>{timeout_seconds}s）") from exc
    stdout_text = stdout.decode("utf-8", errors="ignore").strip()
    stderr_text = stderr.decode("utf-8", errors="ignore").strip()
    if proc.returncode != 0:
        raise WorkflowRuntimeError(stderr_text or "脚本执行失败")
    parsed = _safe_json_loads(stdout_text, {})
    return {
        "ok": True,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "result": parsed.get("result") if isinstance(parsed, dict) else stdout_text,
    }


async def _execute_node_logic(
    *,
    current_id: str,
    node: dict,
    state: WorkflowGraphState,
    node_map: dict[str, dict],
    outgoing: dict[str, list[dict]],
    tenant_id: str,
    tenant_name: str,
    model_settings: dict,
    rag_runtime,
    retrieval_settings: dict,
    tool_settings: dict,
    max_depth: int,
    branch_runner: Callable[[str, set[str] | None, set[str] | None], Any],
) -> tuple[dict, str | None]:
    node_type = str(node.get("type") or "").strip()
    node_data = node.get("data") if isinstance(node.get("data"), dict) else {}
    context = {
        "input": state["input"],
        "state": state,
        "nodes": state["nodes"],
        "last": state["last_result"],
        "workflow": state["workflow"],
        "now": _now_text(),
    }
    result: dict = {"ok": True}
    next_id: str | None = None

    if node_type == "start":
        result = {
            "ok": True,
            "triggerType": str(node_data.get("triggerType") or "手动触发"),
            "payload": deepcopy(state["input"]),
        }
    elif node_type == "ai":
        rendered_prompt = str(_render_template(node_data.get("prompt") or "{{input.text}}", context))
        used_fallback = False
        fallback_reason = ""
        input_images = state["input"].get("images") if isinstance(state.get("input"), dict) else []
        try:
            llm_result = await _call_llm(
                prompt=rendered_prompt,
                model_settings=model_settings,
                node_data=node_data,
                images=input_images if isinstance(input_images, list) else [],
            )
        except WorkflowRuntimeError as exc:
            used_fallback = True
            fallback_reason = str(exc)
            llm_result = {
                "model": "__workflow_fallback__",
                "provider_label": "local_fallback",
                "text": _build_ai_fallback_text(node_data=node_data, context=context, reason=fallback_reason),
                "raw": {"fallback": True, "reason": fallback_reason},
            }
        structured = _normalize_ai_structured_output(llm_result.get("text", ""))
        answer_text = structured.get("answer_text") if isinstance(structured.get("answer_text"), str) else ""
        result = {
            "ok": True,
            "prompt": rendered_prompt,
            "model": llm_result.get("model", ""),
            "provider": llm_result.get("provider_label", ""),
            "text": answer_text or llm_result.get("text", ""),
            "raw_text": llm_result.get("text", ""),
            "raw": llm_result.get("raw", {}),
            "structured_output": structured.get("parsed") if isinstance(structured.get("parsed"), dict) else {},
            "render_payload": structured.get("render_payload") if isinstance(structured.get("render_payload"), dict) else {},
            "fallback_used": used_fallback,
            "fallback_reason": fallback_reason,
        }
    elif node_type == "knowledge":
        query = str(_render_template(node_data.get("query") or "{{input.text}}", context)).strip()
        top_k = max(1, int(_to_number(node_data.get("topK"), 5)))
        knowledge_result = _run_workflow_knowledge_search(
            tenant_id=tenant_id,
            query=query,
            top_k=top_k,
            node_data=node_data,
            rag_runtime=rag_runtime,
            retrieval_config=retrieval_settings,
        )
        filtered = list(knowledge_result.get("hits") or [])
        result = {
            "ok": True,
            "query": str(knowledge_result.get("query") or query),
            "top_k": top_k,
            "hits": filtered,
            "knowledge_text": "\n\n".join(str(item.get("content") or "") for item in filtered),
            "retrieval_backend": str(knowledge_result.get("retrieval_backend") or ""),
            "retrieval_route": dict(knowledge_result.get("retrieval_route") or {}),
            "retrieval_attempts": int(knowledge_result.get("retrieval_attempts") or 1),
            "retrieval_judge": dict(knowledge_result.get("retrieval_judge") or {}),
            "retrieval_strategy_trace": list(knowledge_result.get("retrieval_strategy_trace") or []),
            "query_profile": str(knowledge_result.get("query_profile") or ""),
        }
    elif node_type == "condition":
        expression = str(node_data.get("condition") or "").strip()
        matched = _eval_condition(expression, context)
        result = {
            "ok": True,
            "expression": expression,
            "matched": matched,
        }
        branches = list(outgoing.get(current_id, []))
        next_id = str((branches[0] if matched else (branches[1] if len(branches) > 1 else {})).get("to") or "") or None
    elif node_type == "http":
        url = str(_render_template(node_data.get("url") or "", context)).strip()
        method = str(node_data.get("method") or "GET").strip().upper()
        headers = _safe_json_loads(_render_template(node_data.get("headers") or "", context), {})
        body = _render_template(node_data.get("body") or "", context)
        json_body = _safe_json_loads(body, None)
        try:
            async with acquire_workflow_io_slot():
                connector = aiohttp.TCPConnector(ssl=WORKFLOW_SSL_CTX)
                async with aiohttp.ClientSession(connector=connector) as session:
                    async with session.request(
                        method,
                        url,
                        headers=headers if isinstance(headers, dict) else {},
                        json=json_body if isinstance(json_body, (dict, list)) else None,
                        data=None if isinstance(json_body, (dict, list)) else (str(body) if body else None),
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as resp:
                        text = await resp.text()
                        parsed = _safe_json_loads(text, {})
                        result = {
                            "ok": resp.status < 400,
                            "status": resp.status,
                            "url": url,
                            "method": method,
                            "headers": dict(resp.headers),
                            "body": parsed if parsed else text,
                        }
        except BusyError:
            result = {
                "ok": False,
                "status": 503,
                "url": url,
                "method": method,
                "headers": {},
                "body": {"ok": False, "message": "工作流HTTP通道繁忙"},
            }
    elif node_type == "script":
        result = await _run_script(node_data, context)
    elif node_type == "notify":
        channel = str(node_data.get("channel") or "站内推送").strip()
        to_value = str(_render_template(node_data.get("to") or "", context)).strip()
        title = str(_render_template(node_data.get("title") or "", context)).strip()
        content = str(_render_template(node_data.get("content") or "", context)).strip()
        delivery = {"ok": True, "channel": channel, "to": to_value, "title": title, "content": content}
        if channel == "邮件" and to_value:
            delivery = {"channel": channel, "to": to_value, "title": title, "content": content}
            delivery.update(await _send_email(tool_settings, to_email=to_value, subject=title or "工作流通知", content=content))
        state["notifications"].append(delivery)
        result = delivery
    elif node_type == "mcp":
        result = await _call_mcp_server(
            tool_cfg=tool_settings,
            node_data=node_data,
            context=context,
            tenant_id=tenant_id,
            tenant_name=tenant_name,
        )
    elif node_type == "delay":
        duration = max(0, _to_number(node_data.get("duration"), 0))
        unit = str(node_data.get("timeUnit") or "秒").strip()
        multiplier = {"秒": 1, "分钟": 60, "小时": 3600, "天": 86400}.get(unit, 1)
        actual_seconds = duration * multiplier
        sleep_seconds = min(actual_seconds, 5)
        if sleep_seconds > 0:
            await asyncio.sleep(sleep_seconds)
        result = {
            "ok": True,
            "requested_seconds": actual_seconds,
            "slept_seconds": sleep_seconds,
        }
    elif node_type == "form":
        fields = _safe_json_loads(node_data.get("fields") or "[]", [])
        form_values = state["input"].get("forms", {}).get(current_id)
        if form_values is None:
            form_values = state["input"].get("form", {})
        state["forms"][current_id] = {"schema": fields, "values": deepcopy(form_values or {})}
        result = {
            "ok": True,
            "formName": str(node_data.get("formName") or _node_label(node)),
            "fields": fields,
            "values": deepcopy(form_values or {}),
        }
    elif node_type == "parallel":
        branch_conns = list(outgoing.get(current_id, []))
        branch_ids = [str(item.get("to") or "") for item in branch_conns if str(item.get("to") or "").strip()]
        merge_id = _find_merge_node(branch_ids, outgoing)
        branch_tasks = [
            branch_runner(branch_id, stop_before={merge_id} if merge_id else set(), stack=set())
            for branch_id in branch_ids
        ]
        branch_results = await asyncio.gather(*branch_tasks) if branch_tasks else []
        normalized_results = []
        for item in branch_results:
            if isinstance(item, dict) and isinstance(item.get("graph_state"), dict):
                _merge_runtime_state(
                    state,
                    item.get("graph_state") or {},
                    base_log_count=int(item.get("base_log_count") or 0),
                    base_notification_count=int(item.get("base_notification_count") or 0),
                    base_node_keys={str(key) for key in item.get("base_node_keys") or []},
                    base_form_keys={str(key) for key in item.get("base_form_keys") or []},
                )
                normalized_results.append(
                    {
                        "last_result": deepcopy(item.get("last_result") or {}),
                        "stopped_at": str(item.get("stopped_at") or ""),
                    }
                )
            else:
                normalized_results.append(item)
        result = {
            "ok": True,
            "branches": normalized_results,
            "merge_node_id": merge_id or "",
        }
        next_id = merge_id
    elif node_type == "loop":
        loop_type = str(node_data.get("loopType") or "次数循环").strip()
        loop_count = max(0, int(_to_number(node_data.get("loopCount"), 0)))
        branch_conns = list(outgoing.get(current_id, []))
        body_id = str((branch_conns[0] if branch_conns else {}).get("to") or "")
        exit_id = str((branch_conns[1] if len(branch_conns) > 1 else {}).get("to") or "")
        runs = []
        if loop_type == "次数循环" and body_id:
            for index in range(loop_count):
                state["input"]["loop_index"] = index
                branch_result = await branch_runner(body_id, stop_before={current_id}, stack=set())
                if isinstance(branch_result, dict) and isinstance(branch_result.get("graph_state"), dict):
                    _merge_runtime_state(
                        state,
                        branch_result.get("graph_state") or {},
                        base_log_count=int(branch_result.get("base_log_count") or 0),
                        base_notification_count=int(branch_result.get("base_notification_count") or 0),
                        base_node_keys={str(key) for key in branch_result.get("base_node_keys") or []},
                        base_form_keys={str(key) for key in branch_result.get("base_form_keys") or []},
                    )
                    branch_result = {
                        "last_result": deepcopy(branch_result.get("last_result") or {}),
                        "stopped_at": str(branch_result.get("stopped_at") or ""),
                    }
                runs.append(branch_result)
        elif loop_type == "条件循环" and body_id:
            max_turns = max(1, int(_to_number(node_data.get("maxTurns"), 10)))
            for index in range(max_turns):
                state["input"]["loop_index"] = index
                loop_context = {
                    "input": state["input"],
                    "state": state,
                    "nodes": state["nodes"],
                    "last": state["last_result"],
                    "workflow": state["workflow"],
                    "now": _now_text(),
                }
                if not _eval_condition(str(node_data.get("condition") or ""), loop_context):
                    break
                branch_result = await branch_runner(body_id, stop_before={current_id}, stack=set())
                if isinstance(branch_result, dict) and isinstance(branch_result.get("graph_state"), dict):
                    _merge_runtime_state(
                        state,
                        branch_result.get("graph_state") or {},
                        base_log_count=int(branch_result.get("base_log_count") or 0),
                        base_notification_count=int(branch_result.get("base_notification_count") or 0),
                        base_node_keys={str(key) for key in branch_result.get("base_node_keys") or []},
                        base_form_keys={str(key) for key in branch_result.get("base_form_keys") or []},
                    )
                    branch_result = {
                        "last_result": deepcopy(branch_result.get("last_result") or {}),
                        "stopped_at": str(branch_result.get("stopped_at") or ""),
                    }
                runs.append(branch_result)
        result = {
            "ok": True,
            "loopType": loop_type,
            "iterations": len(runs),
            "runs": runs,
        }
        next_id = exit_id or None
    elif node_type == "subflow":
        subflow_id = str(node_data.get("subflowId") or "").strip()
        subflow_input = _safe_json_loads(_render_template(node_data.get("input") or "{}", context), {})
        subflow_result = await execute_tenant_workflow(
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            workflow_id=subflow_id,
            input_payload=subflow_input if isinstance(subflow_input, dict) else {"value": subflow_input},
            max_depth=max_depth - 1,
        )
        result = {
            "ok": True,
            "subflow_id": subflow_id,
            "subflow_result": subflow_result,
        }
    elif node_type == "end":
        result = {
            "ok": True,
            "endType": str(node_data.get("endType") or "正常结束"),
            "message": str(_render_template(node_data.get("endMessage") or "", context)).strip(),
        }
        next_id = None
    else:
        result = {"ok": True, "msg": f"未识别节点类型 {node_type}，已跳过"}

    if next_id is None and node_type not in {"condition", "parallel", "loop", "end"}:
        next_candidates = [str(item.get("to") or "") for item in outgoing.get(current_id, []) if str(item.get("to") or "").strip()]
        next_id = next_candidates[0] if next_candidates else None
    return result, next_id


async def execute_tenant_workflow(
    *,
    tenant_id: str,
    tenant_name: str,
    workflow_id: str | None,
    input_payload: dict | None = None,
    max_depth: int = 3,
) -> dict:
    config = load_workflow_config(tenant_id=tenant_id, tenant_name=tenant_name)
    workflow_items = list(config.get("items") or [])
    target_id = str(workflow_id or config.get("default_workflow_id") or "").strip()
    workflow = next((item for item in workflow_items if item.get("workflow_id") == target_id), None)
    if workflow is None:
        raise WorkflowRuntimeError("未找到对应工作流")
    if not workflow.get("enabled", True):
        raise WorkflowRuntimeError("当前工作流未启用")
    if max_depth <= 0:
        raise WorkflowRuntimeError("子流程嵌套层级过深")

    app_settings = load_tenant_app_config(tenant_id, tenant_name)
    model_settings = load_model_config(tenant_id=tenant_id, tenant_name=tenant_name)
    retrieval_settings = load_retrieval_config(tenant_id=tenant_id, tenant_name=tenant_name)
    tool_settings = load_tool_config(tenant_id=tenant_id, tenant_name=tenant_name)
    rag_runtime = build_runtime_rag_engine(
        knowledge_dir=get_tenant_knowledge_dir(tenant_id),
        app_config=app_settings,
        retrieval_config=retrieval_settings,
    )
    system_prompt = load_tenant_system_prompt(tenant_id, tenant_name)
    nodes = list(workflow.get("nodes") or [])
    connections = list(workflow.get("connections") or [])
    node_map = {str(node.get("id") or ""): node for node in nodes}
    outgoing = _outgoing_map(connections)
    incoming = _incoming_map(connections)
    start_node = _find_start_node(nodes)
    logs: list[dict] = []
    runtime_state: WorkflowGraphState = {
        "input": deepcopy(input_payload or {}),
        "nodes": {},
        "last_result": {},
        "notifications": [],
        "forms": {},
        "workflow": {
            "id": workflow.get("workflow_id"),
            "name": workflow.get("name"),
        },
        "logs": logs,
        "next_node_id": "",
        "entry_node_id": str(start_node.get("id") or ""),
        "stop_before": [],
        "stopped_at": "",
    }

    async def run_path_legacy(node_id: str, *, stop_before: set[str] | None = None, stack: set[str] | None = None) -> dict:
        stop_before = stop_before or set()
        stack = stack or set()
        current_id = node_id
        last_result: dict = {}
        while current_id:
            if current_id in stop_before:
                return {"last_result": last_result, "stopped_at": current_id}
            if current_id in stack:
                raise WorkflowRuntimeError(f"检测到循环依赖: {current_id}")
            stack.add(current_id)
            node = node_map.get(current_id)
            if not node:
                raise WorkflowRuntimeError(f"节点不存在: {current_id}")
            started_at = time.time()
            result, next_id = await _execute_node_logic(
                current_id=current_id,
                node=node,
                state=runtime_state,
                node_map=node_map,
                outgoing=outgoing,
                tenant_id=tenant_id,
                tenant_name=tenant_name,
                model_settings=model_settings,
                rag_runtime=rag_runtime,
                retrieval_settings=retrieval_settings,
                tool_settings=tool_settings,
                max_depth=max_depth,
                branch_runner=run_path_legacy,
            )
            node_type = str(node.get("type") or "").strip()
            finished_at = time.time()
            runtime_state["nodes"][current_id] = {
                "node_id": current_id,
                "type": node_type,
                "label": _node_label(node),
                "result": deepcopy(result),
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": int((finished_at - started_at) * 1000),
            }
            runtime_state["last_result"] = deepcopy(result)
            logs.append(
                {
                    "node_id": current_id,
                    "label": _node_label(node),
                    "type": node_type,
                    "result": deepcopy(result),
                    "duration_ms": int((finished_at - started_at) * 1000),
                }
            )
            last_result = result
            stack.remove(current_id)
            if not next_id:
                return {"last_result": last_result, "stopped_at": ""}
            current_id = next_id
        return {"last_result": last_result, "stopped_at": ""}

    async def _run_langgraph(initial_state: WorkflowGraphState) -> WorkflowGraphState:
        if not LANGGRAPH_AVAILABLE or StateGraph is None:
            raise WorkflowRuntimeError("LangGraph 不可用")
        graph = StateGraph(WorkflowGraphState)
        path_map = {node_id: node_id for node_id in node_map.keys()}
        path_map["__end__"] = END
        compiled = None

        async def run_path_langgraph(node_id: str, *, stop_before: set[str] | None = None, stack: set[str] | None = None) -> dict:
            if compiled is None:
                return await run_path_legacy(node_id, stop_before=stop_before, stack=stack)
            base_log_count = len(runtime_state["logs"])
            base_notification_count = len(runtime_state["notifications"])
            base_node_keys = set(runtime_state["nodes"].keys())
            base_form_keys = set(runtime_state["forms"].keys())
            branch_state = deepcopy(runtime_state)
            branch_state["entry_node_id"] = node_id
            branch_state["stop_before"] = sorted(stop_before or set())
            branch_state["stopped_at"] = ""
            branch_state["next_node_id"] = node_id
            final_branch_state = await compiled.ainvoke(branch_state)
            return {
                "last_result": deepcopy(final_branch_state.get("last_result") or {}),
                "stopped_at": str(final_branch_state.get("stopped_at") or ""),
                "graph_state": final_branch_state,
                "base_log_count": base_log_count,
                "base_notification_count": base_notification_count,
                "base_node_keys": sorted(base_node_keys),
                "base_form_keys": sorted(base_form_keys),
            }

        for workflow_node in nodes:
            node_id = str(workflow_node.get("id") or "")
            if not node_id:
                continue

            async def _node_runner(state: WorkflowGraphState, *, _node=workflow_node, _node_id=node_id):
                started_at = time.time()
                result, next_id = await _execute_node_logic(
                    current_id=_node_id,
                    node=_node,
                    state=state,
                    node_map=node_map,
                    outgoing=outgoing,
                    tenant_id=tenant_id,
                    tenant_name=tenant_name,
                    model_settings=model_settings,
                    rag_runtime=rag_runtime,
                    retrieval_settings=retrieval_settings,
                    tool_settings=tool_settings,
                    max_depth=max_depth,
                    branch_runner=run_path_langgraph,
                )
                finished_at = time.time()
                node_type = str(_node.get("type") or "").strip()
                state["nodes"][_node_id] = {
                    "node_id": _node_id,
                    "type": node_type,
                    "label": _node_label(_node),
                    "result": deepcopy(result),
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_ms": int((finished_at - started_at) * 1000),
                }
                state["last_result"] = deepcopy(result)
                state["logs"].append(
                    {
                        "node_id": _node_id,
                        "label": _node_label(_node),
                        "type": node_type,
                        "result": deepcopy(result),
                        "duration_ms": int((finished_at - started_at) * 1000),
                    }
                )
                stop_before = {str(item or "").strip() for item in state.get("stop_before") or [] if str(item or "").strip()}
                if next_id and next_id in stop_before:
                    state["stopped_at"] = str(next_id)
                    state["next_node_id"] = "__end__"
                else:
                    state["next_node_id"] = str(next_id or "__end__")
                return state

            def _route_next(state: WorkflowGraphState):
                target = str(state.get("next_node_id") or "__end__")
                return target if target in path_map else "__end__"

            graph.add_node(node_id, _node_runner)
            graph.add_conditional_edges(node_id, _route_next, path_map)

        def _route_entry(state: WorkflowGraphState):
            entry_id = str(state.get("entry_node_id") or start_node.get("id") or "__end__")
            return entry_id if entry_id in path_map else "__end__"

        graph.add_conditional_edges(START, _route_entry, path_map)
        compiled = graph.compile()
        return await compiled.ainvoke(initial_state)

    if LANGGRAPH_AVAILABLE and StateGraph is not None:
        final_state = await _run_langgraph(runtime_state)
        final_result = final_state.get("last_result") or {}
        orchestration_backend = "langgraph"
    else:
        final = await run_path_legacy(str(start_node.get("id") or ""))
        final_result = final.get("last_result") or {}
        orchestration_backend = "legacy"
    return {
        "ok": True,
        "workflow_id": workflow.get("workflow_id"),
        "workflow_name": workflow.get("name"),
        "default_prompt": system_prompt,
        "logs": logs,
        "state": runtime_state,
        "final_result": final_result,
        "node_count": len(nodes),
        "connection_count": len(connections),
        "entry_node_id": start_node.get("id"),
        "default_workflow_id": config.get("default_workflow_id"),
        "incoming_count": {key: len(value) for key, value in incoming.items()},
        "orchestration_backend": orchestration_backend,
    }
