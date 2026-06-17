"""FastAPI main application."""
import asyncio
import contextvars
import json
import os
import ssl
import time
import shutil
import uuid
from pathlib import Path
from collections import defaultdict
from contextlib import asynccontextmanager

# SSL: configurable via VERIFY_SSL env var.
# Default: disabled (False) because macOS Python often has broken CA bundles.
# Set VERIFY_SSL=1 to enable strict verification.
_verify_ssl = os.environ.get("VERIFY_SSL", "0").strip()
if _verify_ssl == "1":
    try:
        import certifi
        _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        _ssl_ctx = ssl.create_default_context()
        _ssl_ctx.check_hostname = False
        _ssl_ctx.verify_mode = ssl.CERT_NONE
else:
    _ssl_ctx = False  # aiohttp: False = skip SSL verification
from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from backend.config import (
    LLM_BASE_URL,
    GLOBAL_CHAT_CONCURRENCY_LIMIT,
    GLOBAL_LLM_CONCURRENCY_LIMIT,
    RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS,
    ADMIN_USERNAME, ADMIN_PASSWORD, HOST, PORT, PLATFORM_CRAWLER_CONFIG_PATH,
)
from backend.app_config import (
    APP_CONFIG_PATH,
    build_public_app_config,
    ensure_app_config_file,
    get_deployment_mode,
    get_knowledge_tiers,
    get_public_app_config,
    get_runtime_knowledge_dir,
    load_app_config,
    save_app_config,
)
from backend.release_profiles import export_release_bundle, list_release_profiles
from backend.database import (
    init_db, verify_phone_login, reset_device_binding, get_phone_info,
    list_phone_accounts, check_balance, deduct_balance,
    add_balance_by_phone, search_by_phone, generate_temp_password, change_password,
    list_chat_logs, record_chat_log, record_request_log, record_guardrail_event,
    get_observability_summary, list_request_logs, list_guardrail_events,
    record_crawler_run, list_crawler_runs, clear_crawler_runs,
    record_evaluation_run, list_evaluation_runs,
    list_tenants, create_tenant, update_tenant, verify_tenant_admin,
    list_tenant_phone_accounts, save_tenant_phone_account, toggle_tenant_phone_account,
    create_chat_session, list_chat_sessions, list_chat_session_messages, cleanup_empty_chat_sessions,
    list_agents, get_agent, save_agent, toggle_agent, delete_agent,
    list_user_agent_bindings, save_user_agent_bindings, get_default_agent,
    ensure_agent_publish_api_key, regenerate_agent_publish_api_key, get_agent_by_publish_api_key,
    get_tenant_analytics_summary, get_tenant_daily_trends, get_tenant_agent_usage,
    get_tenant_top_questions, get_tenant_active_users, get_tenant_hourly_distribution,
    list_chat_annotations, save_chat_annotation, get_chat_annotation_summary, get_chat_annotation_label_distribution,
    get_platform_analytics_overview,
)
from backend.model_config import (
    apply_model_selection,
    ensure_model_config_files,
    load_model_config,
    resolve_model_capability,
    save_model_config,
)
from backend.crawler_config import ensure_crawler_config_file, load_crawler_sources, save_crawler_sources
from backend.rag import rag_engine, build_runtime_rag_engine
from backend.cache import semantic_cache
from backend.guardrails import apply_input_guardrails, apply_output_guardrails
from backend.prompt_config import (
    ensure_system_prompt_file,
    load_system_prompt_template,
    load_system_prompt_template_from_path,
    SYSTEM_PROMPT_PATH,
)
from backend.chat_workflow import (
    build_knowledge_hits,
    build_retrieval_trace,
    run_chat_workflow,
    run_chat_workflow_with_runtime,
)
from backend.llm_service import stream_chat_completion
from backend.retrieval_config import (
    ensure_retrieval_config_file,
    load_retrieval_config,
    save_retrieval_config,
    RETRIEVAL_CONFIG_PATH,
)
from backend.tool_config import ensure_tool_config_file, list_enabled_mcp_servers, load_tool_config, save_tool_config
from backend.workflow_config import ensure_workflow_config_file, load_workflow_config, save_workflow_config
from backend.workflow_runtime import WorkflowRuntimeError, execute_tenant_workflow
from backend.concurrency import BusyError, acquire_chat_slots, get_concurrency_snapshot
from backend.security_config import ensure_security_config_file, load_security_config
from backend.knowledge_assets import (
    delete_knowledge_file_meta,
    get_knowledge_file_meta,
    list_knowledge_categories,
    list_knowledge_libraries,
    list_knowledge_tags,
    list_knowledge_tag_groups,
    save_knowledge_structure,
    save_knowledge_tag_catalog,
    save_knowledge_tag_groups,
    set_knowledge_file_meta,
    set_knowledge_file_tags,
)
from backend.tenant_config import (
    ensure_tenant_storage,
    get_tenant_paths,
    get_tenant_knowledge_dir,
    load_tenant_app_config,
    load_tenant_system_prompt,
    save_tenant_app_config,
    save_tenant_system_prompt,
)
from backend.generic_crawler import GenericCrawlerError, run_generic_crawler
from backend.scheduler import crawler_scheduler
from backend.langchain_components import langchain_runtime_status
from backend.document_processing import (
    ParsedKnowledgeFile,
    UnsupportedDocumentError,
    normalize_tier,
    parse_uploaded_knowledge_file,
    split_documents_for_stats,
)
from backend.retrieval_evaluation import run_retrieval_evaluation

try:
    from backend.hospital_mock import mock_hospital_mcp_result
except Exception:  # pragma: no cover
    mock_hospital_mcp_result = None

PUBLIC_TIER_CODE_MAP = {
    "permanent": "L1",
    "seasonal": "L2",
    "hotfix": "L3",
}


def _canonical_tier(value: str) -> str:
    return normalize_tier(value)

def _paginate_rows(rows: list[dict], page: int, page_size: int) -> tuple[list[dict], int]:
    safe_page = max(1, int(page or 1))
    safe_page_size = max(1, min(int(page_size or 20), 200))
    total = len(rows)
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    return rows[start:end], total


def _public_tier_code(value: str) -> str:
    canonical = _canonical_tier(value)
    return PUBLIC_TIER_CODE_MAP.get(canonical, value)

def user_facing_llm_error() -> str:
    """Return a safe, friendly message for frontend users."""
    return "服务暂时繁忙，我正在自动切换备用线路。请稍等几秒后再试。"


def _now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

# --- Rate Limiter (1 req / 10s per user key) ---
rate_limit_store: dict[str, list[float]] = defaultdict(list)
request_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="")

# --- Concurrency Tracking (for queue status) ---
_active_llm_calls = 0
_MAX_CONCURRENT_LLM = max(1, GLOBAL_LLM_CONCURRENCY_LIMIT)  # queue display threshold

def check_rate_limit(key: str) -> bool:
    now = time.time()
    window = rate_limit_store[key]
    rate_limit_store[key] = [t for t in window if now - t < RATE_LIMIT_WINDOW_SECONDS]
    if len(rate_limit_store[key]) >= RATE_LIMIT_MAX_REQUESTS:
        return False
    rate_limit_store[key].append(now)
    return True


def _busy_response_message(scope: str) -> str:
    if scope == "agent_chat":
        return "当前智能体咨询人数较多，请稍后再试。"
    if scope == "tenant_chat":
        return "当前租户咨询人数较多，请稍后再试。"
    if scope == "llm":
        return "当前模型通道繁忙，请稍后再试。"
    if scope == "workflow_io":
        return "当前外部接口繁忙，请稍后再试。"
    return "当前咨询人数较多，请稍后再试。"


def _busy_json_response(request: Request, exc: BusyError):
    request.state.ob_error_message = f"busy:{exc.scope}:{exc.current}/{exc.limit}"
    return JSONResponse(
        {
            "ok": False,
            "msg": _busy_response_message(exc.scope),
            "busy_scope": exc.scope,
            "limit": exc.limit,
            "current": exc.current,
        },
        status_code=503,
    )

# --- Admin Auth ---
def verify_admin(request: Request):
    auth = request.headers.get("Authorization", "")
    import base64
    expected = base64.b64encode(f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}".encode()).decode()
    if auth != f"Basic {expected}":
        raise HTTPException(status_code=401, detail="管理员认证失败")


def verify_tenant_admin_request(request: Request) -> dict:
    """校验租户后台的 Basic 认证。"""
    auth = request.headers.get("Authorization", "")
    import base64

    if not auth.startswith("Basic "):
        raise HTTPException(status_code=401, detail="租户管理员认证失败")
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        username, password = decoded.split(":", 1)
    except Exception as exc:  # pragma: no cover - 非法头部
        raise HTTPException(status_code=401, detail="租户管理员认证失败") from exc
    result = verify_tenant_admin(username, password)
    if not result.get("ok"):
        raise HTTPException(status_code=401, detail=result.get("msg", "租户管理员认证失败"))
    request.state.ob_tenant_id = str(result.get("tenant_id") or "default").strip() or "default"
    request.state.ob_user_phone = str(result.get("admin_username") or username or "").strip()
    return result

# --- Lifespan ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    ensure_app_config_file()
    ensure_model_config_files()
    ensure_crawler_config_file()
    ensure_retrieval_config_file()
    ensure_tool_config_file()
    ensure_workflow_config_file()
    ensure_security_config_file()
    ensure_system_prompt_file()
    runtime_status = langchain_runtime_status()
    print(f"[Runtime] LangChain loaders: {runtime_status}")
    missing_loaders = []
    if not runtime_status.get("pdf_loader"):
        missing_loaders.append("PDF")
    if not runtime_status.get("word_loader"):
        missing_loaders.append("DOCX")
    if missing_loaders:
        print(f"[WARN] 文档解析能力缺失: {', '.join(missing_loaders)}。请确认服务使用 lok/venv/bin/python 启动。")
    count = rag_engine.build_index()
    print(f"[RAG] Knowledge base initialized: {count} chunks")
    crawler_scheduler.start()
    yield
    await crawler_scheduler.stop()

app = FastAPI(title="企业级 RAG Agent 平台", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

@app.middleware("http")
async def add_no_cache_headers(request: Request, call_next):
    request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
    request_id_ctx.set(request_id)
    request.state.request_id = request_id
    request.state.started_at = time.perf_counter()
    request.state.ob_user_phone = ""
    request.state.ob_tenant_id = "default"
    request.state.ob_cache_status = "miss"
    request.state.ob_model_name = ""
    request.state.ob_error_message = ""
    response = await call_next(request)
    duration_ms = int((time.perf_counter() - request.state.started_at) * 1000)
    response.headers["X-Request-Id"] = request_id
    if request.url.path in ("/", "/admin", "/tenant", "/chat"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    try:
        record_request_log(
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            status_code=response.status_code,
            duration_ms=duration_ms,
            client_ip=(request.client.host if request.client else ""),
            phone=getattr(request.state, "ob_user_phone", "") or "",
            cache_status=getattr(request.state, "ob_cache_status", "") or "",
            model_name=getattr(request.state, "ob_model_name", "") or "",
            error_message=getattr(request.state, "ob_error_message", "") or "",
            tenant_id=getattr(request.state, "ob_tenant_id", "default") or "default",
        )
    except Exception as exc:
        # Observability logging should never break page rendering or API responses.
        print(f"[WARN] record_request_log failed: {exc}")
    return response

FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")
INGEST_REPORT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "ingest", "last_ingest_report.json")


def _knowledge_tiers() -> dict:
    return get_knowledge_tiers()


def _knowledge_dir() -> str:
    root = get_runtime_knowledge_dir()
    os.makedirs(root, exist_ok=True)
    return root


def _iter_knowledge_markdown_files(root: str) -> list[str]:
    base = Path(root)
    if not base.exists():
        return []
    files: list[str] = []
    for path in base.rglob("*.md"):
        if any(part.startswith(".") for part in path.parts if part not in {".", ".."}):
            continue
        if ".upload_tmp" in path.parts:
            continue
        if path.is_file():
            files.append(str(path))
    return sorted(files)


def _safe_storage_segment(value: str, fallback: str) -> str:
    clean = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or "").strip())
    clean = clean.strip("_")
    return clean or fallback


def _knowledge_storage_dir(root: str, library_id: str = "", category_id: str = "") -> str:
    library_segment = _safe_storage_segment(library_id, "kb_default")
    category_segment = _safe_storage_segment(category_id, "uncategorized") if category_id else ""
    target = Path(root) / library_segment
    if category_segment:
        target = target / category_segment
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def _find_knowledge_file_path(root: str, file_name: str) -> str:
    clean_name = Path(str(file_name or "")).name
    if not clean_name:
        return ""
    for fpath in _iter_knowledge_markdown_files(root):
        if Path(fpath).name == clean_name:
            return fpath
    return ""


def _is_single_backend_mode() -> bool:
    """当前是否为企业版单后台模式。"""
    return get_deployment_mode() == "single_backend"


def _tenant_runtime_bundle(tenant: dict) -> dict:
    """构建租户级运行时配置。

    这里显式把知识目录、Prompt、模型与检索配置隔离开，避免租户问答继续误用平台默认配置。
    """
    tenant_id = str(tenant.get("tenant_id") or "default").strip() or "default"
    tenant_name = str(tenant.get("tenant_name") or "").strip()
    app_settings = load_tenant_app_config(tenant_id, tenant_name)
    model_settings = load_model_config(tenant_id=tenant_id, tenant_name=tenant_name)
    retrieval_settings = load_retrieval_config(tenant_id=tenant_id, tenant_name=tenant_name)
    retrieval_settings = json.loads(json.dumps(retrieval_settings))
    qdrant_cfg = retrieval_settings.setdefault("qdrant", {})
    milvus_cfg = retrieval_settings.setdefault("milvus", {})
    # 租户运行时始终绑定自己的 collection，避免误用平台默认向量集合。
    qdrant_cfg["collection"] = f"tenant_{tenant_id}_knowledge"
    milvus_cfg["collection"] = f"tenant_{tenant_id}_knowledge"
    knowledge_dir = get_tenant_knowledge_dir(tenant_id)
    prompt_path = get_tenant_paths(tenant_id)["prompt"]
    rag_runtime = build_runtime_rag_engine(
        knowledge_dir=knowledge_dir,
        app_config=app_settings,
        retrieval_config=retrieval_settings,
    )
    return {
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
        "knowledge_dir": knowledge_dir,
        "app_settings": app_settings,
        "model_settings": model_settings,
        "retrieval_settings": retrieval_settings,
        "prompt_template": load_system_prompt_template_from_path(prompt_path),
        "rag_runtime": rag_runtime,
    }


def _normalize_public_questions(value) -> list[str]:
    items = value if isinstance(value, list) else []
    result: list[str] = []
    for item in items:
        clean = str(item or "").strip()
        if clean:
            result.append(clean)
    return result[:8]


def _normalize_chat_images(value: object, *, max_items: int = 4, max_data_url_length: int = 8_000_000) -> list[dict]:
    if not isinstance(value, list):
        return []
    result: list[dict] = []
    for item in value[:max_items]:
        if not isinstance(item, dict):
            continue
        data_url = str(item.get("data_url") or "").strip()
        if not data_url.startswith("data:image/"):
            continue
        if len(data_url) > max_data_url_length:
            continue
        mime_type = str(item.get("mime_type") or "image/jpeg").strip() or "image/jpeg"
        name = str(item.get("name") or "image").strip() or "image"
        result.append({"data_url": data_url, "mime_type": mime_type, "name": name})
    return result


def _apply_agent_model_override(model_settings: dict, agent: dict | None) -> dict:
    if not agent:
        return model_settings
    model_name = str((agent.get("model_override") or {}).get("model") or agent.get("model") or "").strip()
    if not model_name:
        return model_settings
    return apply_model_selection(model_settings, model_name)


def _agent_model_capability(agent: dict | None) -> dict:
    if not agent:
        return {"model": "", "supports_image": False, "capability_label": "文本"}
    tenant_id = str(agent.get("tenant_id") or "").strip()
    if not tenant_id:
        return {"model": "", "supports_image": False, "capability_label": "文本"}
    model_settings = load_model_config(tenant_id=tenant_id, tenant_name=str(agent.get("tenant_name") or tenant_id))
    model_name = str((agent.get("model_override") or {}).get("model") or agent.get("model") or "").strip()
    return resolve_model_capability(model_settings, model_name)


def _apply_agent_overrides(app_settings: dict, agent: dict | None) -> dict:
    if not agent:
        return app_settings
    settings = json.loads(json.dumps(app_settings))
    if agent.get("name"):
        settings["chat_title"] = agent["name"]
    if agent.get("description"):
        settings["chat_tagline"] = agent["description"]
        if not settings.get("agent_description"):
            settings["agent_description"] = agent["description"]
    if agent.get("welcome_message"):
        settings["welcome_message"] = agent["welcome_message"]
    if agent.get("input_placeholder"):
        settings["input_placeholder"] = agent["input_placeholder"]
    questions = _normalize_public_questions(agent.get("recommended_questions"))
    if questions:
        settings["recommended_questions"] = questions
    if "streaming" in agent:
        settings["streaming"] = bool(agent.get("streaming", True))
    if "fallback_enabled" in agent:
        settings["fallback_enabled"] = bool(agent.get("fallback_enabled", True))
    if str(agent.get("fallback_message") or "").strip():
        settings["fallback_message"] = str(agent.get("fallback_message") or "").strip()
    if "show_recommended" in agent:
        settings["show_recommended"] = bool(agent.get("show_recommended", True))
    if agent.get("avatar"):
        settings["logo"] = agent["avatar"]
    return settings


def _workflow_hits_to_public_items(workflow_result: dict) -> list[dict]:
    items: list[dict] = []
    state = workflow_result.get("state") if isinstance(workflow_result.get("state"), dict) else {}
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    for node in nodes.values():
        if not isinstance(node, dict) or str(node.get("type") or "").strip() != "knowledge":
            continue
        result = node.get("result") if isinstance(node.get("result"), dict) else {}
        for hit in result.get("hits") or []:
            if not isinstance(hit, dict):
                continue
            tier = str(hit.get("tier") or "").strip()
            items.append(
                {
                    "source": str(hit.get("source") or ""),
                    "content": str(hit.get("content") or ""),
                    "score": float(hit.get("score") or 0),
                    "tier": tier,
                    "tier_label": hit.get("tier_label") or _public_tier_code(tier),
                    "tags": list(hit.get("tags") or []),
                    "library_id": str(hit.get("library_id") or ""),
                    "library_name": str(hit.get("library_name") or ""),
                    "category_id": str(hit.get("category_id") or ""),
                    "category_name": str(hit.get("category_name") or ""),
                    "rerank_score": hit.get("rerank_score"),
                    "final_score": hit.get("final_score"),
                    "backend": "workflow",
                }
            )
    return items


def _extract_answer_and_render_from_text(raw_text: object) -> tuple[str, dict]:
    text = str(raw_text or "").strip()
    if not text or not text.startswith("{"):
        return text, {}
    try:
        parsed = json.loads(text)
    except Exception:
        return "", {}
    if not isinstance(parsed, dict):
        return "", {}
    answer_text = ""
    for key in ["answer_text", "answer", "text", "markdown", "content", "summary"]:
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            answer_text = value.strip()
            break
    render_payload = parsed.get("render_payload") if isinstance(parsed.get("render_payload"), dict) else {}
    return answer_text, render_payload


def _workflow_result_to_answer_text(workflow_result: dict) -> str:
    state = workflow_result.get("state") if isinstance(workflow_result.get("state"), dict) else {}
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    for node in reversed(list(nodes.values())):
        if not isinstance(node, dict):
            continue
        result = node.get("result") if isinstance(node.get("result"), dict) else {}
        structured = result.get("structured_output") if isinstance(result.get("structured_output"), dict) else {}
        for key in ["answer_text", "answer", "text", "markdown", "content", "summary"]:
            value = str(structured.get(key) or "").strip()
            if value:
                return value
        node_text = str(result.get("text") or "").strip()
        parsed_text, _ = _extract_answer_and_render_from_text(node_text)
        if parsed_text:
            return parsed_text
        if node_text and not node_text.startswith("{"):
            return node_text
    final_result = workflow_result.get("final_result") if isinstance(workflow_result.get("final_result"), dict) else {}
    structured = final_result.get("structured_output") if isinstance(final_result.get("structured_output"), dict) else {}
    for key in ["answer_text", "answer", "text", "markdown", "content", "summary"]:
        value = str(structured.get(key) or "").strip()
        if value:
            return value
    parsed_text, _ = _extract_answer_and_render_from_text(final_result.get("text") or final_result.get("message") or "")
    if parsed_text:
        return parsed_text
    for key in ["message", "text", "result", "summary"]:
        value = str(final_result.get(key) or "").strip()
        if value and not value.startswith("{"):
            return value
    return ""


def _workflow_result_to_render_payload(workflow_result: dict) -> dict:
    state = workflow_result.get("state") if isinstance(workflow_result.get("state"), dict) else {}
    nodes = state.get("nodes") if isinstance(state.get("nodes"), dict) else {}
    for node in reversed(list(nodes.values())):
        if not isinstance(node, dict):
            continue
        result = node.get("result") if isinstance(node.get("result"), dict) else {}
        explicit_result = result.get("render_payload") if isinstance(result.get("render_payload"), dict) else {}
        if explicit_result:
            return explicit_result
        structured_output = result.get("structured_output") if isinstance(result.get("structured_output"), dict) else {}
        structured_render = structured_output.get("render_payload") if isinstance(structured_output.get("render_payload"), dict) else {}
        if structured_render:
            return structured_render
        _, parsed_render = _extract_answer_and_render_from_text(result.get("text") or "")
        if parsed_render:
            return parsed_render
        payload = result.get("result") if isinstance(result.get("result"), dict) else {}
        if not isinstance(payload, dict):
            continue
        explicit = payload.get("render_payload") if isinstance(payload.get("render_payload"), dict) else {}
        if explicit:
            return explicit
    final_result = workflow_result.get("final_result") if isinstance(workflow_result.get("final_result"), dict) else {}
    structured_output = final_result.get("structured_output") if isinstance(final_result.get("structured_output"), dict) else {}
    structured_render = structured_output.get("render_payload") if isinstance(structured_output.get("render_payload"), dict) else {}
    if structured_render:
        return structured_render
    _, parsed_render = _extract_answer_and_render_from_text(final_result.get("text") or final_result.get("message") or "")
    if parsed_render:
        return parsed_render
    return {}


def _stream_workflow_chat_response(
    *,
    request: Request,
    phone: str,
    question: str,
    workflow_result: dict,
    tenant_id: str,
    agent_id: str = "",
    session_id: str = "",
    app_settings: dict | None = None,
    cache_key: str = "",
    skip_cache_write: bool = False,
):
    effective_app_settings = app_settings or {}
    request.state.ob_orchestration = workflow_result.get("orchestration_backend", "workflow")
    cached = workflow_result.get("cached_answer", "") if workflow_result.get("cache_hit") else ""
    if cached:
        request.state.ob_cache_status = "hit"
        cached_hits = list(workflow_result.get("cached_knowledge_hits") or [])
        cached_trace = dict(workflow_result.get("cached_retrieval_trace") or {})
        cached_trace["phase_timings"] = {
            **dict(cached_trace.get("phase_timings") or {}),
            "cache_hit": True,
        }
        if effective_app_settings.get("record_chat_logs", True):
            record_chat_log(
                phone=phone,
                question=question,
                answer=cached,
                knowledge_hits=cached_hits,
                retrieval_trace=cached_trace,
                tenant_id=tenant_id,
                agent_id=agent_id,
                request_id=request.state.request_id,
                session_id=session_id,
            )
        return _build_cached_stream(
            request=request,
            phone=phone,
            cached_text=cached,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            knowledge_hits=cached_hits,
            retrieval_trace=cached_trace,
        )

    answer_text = _workflow_result_to_answer_text(workflow_result)
    render_payload = _workflow_result_to_render_payload(workflow_result)
    if not answer_text and not render_payload:
        request.state.ob_error_message = "workflow_empty_answer"
        return JSONResponse({"ok": False, "msg": "工作流已执行，但没有生成可返回的答复"}, status_code=500)
    knowledge_hits = build_knowledge_hits(_workflow_hits_to_public_items(workflow_result))
    rerank_score = next(
        (item.get("rerank_score") for item in knowledge_hits if item.get("rerank_score") is not None),
        None,
    )
    retrieval_trace = {
        "retrieval_backend": "workflow",
        "orchestration_backend": workflow_result.get("orchestration_backend", "workflow"),
        "workflow_id": str(workflow_result.get("workflow_id") or ""),
        "workflow_name": str(workflow_result.get("workflow_name") or ""),
        "node_count": int(workflow_result.get("node_count") or 0),
        "connection_count": int(workflow_result.get("connection_count") or 0),
        "knowledge_hits_count": len(knowledge_hits),
        "rerank_enabled": True,
        "rerank_provider": "workflow_inherited",
        "rerank_model": "workflow_inherited",
        "rerank_applied": any(item.get("rerank_score") is not None for item in knowledge_hits),
        "rerank_score": rerank_score,
        "phase_timings": {},
        "render_payload": render_payload,
    }
    if cache_key and not skip_cache_write and answer_text:
        semantic_cache.put(
            cache_key,
            answer_text,
            knowledge_hits=knowledge_hits,
            retrieval_trace=retrieval_trace,
        )
    if effective_app_settings.get("record_chat_logs", True):
        record_chat_log(
            phone=phone,
            question=question,
            answer=answer_text,
            knowledge_hits=knowledge_hits,
            retrieval_trace=retrieval_trace,
            tenant_id=tenant_id,
            agent_id=agent_id,
            request_id=request.state.request_id,
            session_id=session_id,
        )

    async def workflow_stream():
        yield f"data: {json.dumps({'session_id': session_id, 'knowledge_hits': knowledge_hits, 'retrieval_trace': retrieval_trace}, ensure_ascii=False)}\n\n"
        yield f"data: {json.dumps({'status': 'generating', 'content': ''}, ensure_ascii=False)}\n\n"
        if answer_text:
            for i in range(0, len(answer_text), 10):
                chunk = answer_text[i:i + 10]
                guarded, output_events = apply_output_guardrails(chunk)
                for event in output_events:
                    record_guardrail_event(
                        request_id=request.state.request_id,
                        phone=phone,
                        tenant_id=tenant_id,
                        stage=event.get("stage", "output"),
                        action=event.get("action", "mask"),
                        rule_name=event.get("rule", "unknown"),
                        detail=event.get("detail", ""),
                    )
                yield f"data: {json.dumps({'content': guarded}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.02)
        yield "data: [DONE]\n\n"

    return StreamingResponse(workflow_stream(), media_type="text/event-stream")


def _record_failed_workflow_chat_log(
    *,
    request: Request,
    phone: str,
    question: str,
    tenant_id: str,
    agent_id: str = "",
    session_id: str = "",
    workflow_id: str = "",
    workflow_name: str = "",
    error_message: str = "",
    status_code: int = 400,
    failure_stage: str = "workflow_runtime",
):
    """Persist a minimal chat log so failed workflow requests remain traceable in observability views."""
    safe_error = str(error_message or "").strip() or "工作流执行失败"
    try:
        record_chat_log(
            phone=phone,
            question=question,
            answer=safe_error,
            knowledge_hits=[],
            retrieval_trace={
                "retrieval_backend": "workflow",
                "orchestration_backend": "workflow",
                "workflow_id": str(workflow_id or ""),
                "workflow_name": str(workflow_name or workflow_id or ""),
                "knowledge_hits_count": 0,
                "workflow_error": True,
                "failure_stage": failure_stage,
                "status_code": int(status_code or 400),
                "error_message": safe_error,
                "phase_timings": {},
                "render_payload": {},
            },
            tenant_id=tenant_id,
            agent_id=agent_id,
            request_id=request.state.request_id,
            session_id=session_id,
        )
    except Exception as exc:
        print(f"[WARN] record_failed_workflow_chat_log failed: {exc}")


def _extract_hotfix_notice_excerpt(tenant_id: str, agent: dict | None = None) -> str:
    clean_tenant_id = str(tenant_id or "").strip()
    agent_id = str((agent or {}).get("agent_id") or "").strip()
    if not clean_tenant_id or not agent_id.startswith("agent_"):
        return ""
    notice_slug = agent_id[len("agent_"):]
    knowledge_root = Path(get_tenant_knowledge_dir(clean_tenant_id))
    expected_name = f"{notice_slug}_03_本周公告.md"
    notice_path = next((Path(path) for path in _iter_knowledge_markdown_files(str(knowledge_root)) if Path(path).name == expected_name), None)
    if not notice_path or not notice_path.exists():
        return ""
    try:
        content = notice_path.read_text(encoding="utf-8")
    except Exception:
        return ""
    section = content
    if "## 当前公告" in content:
        section = content.split("## 当前公告", 1)[1]
        if "\n## " in section:
            section = section.split("\n## ", 1)[0]
    lines: list[str] = []
    for raw in section.splitlines():
        line = str(raw or "").strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("来源可信度") or line.startswith("是否需人工复核"):
            continue
        lines.append(line.lstrip("-").strip())
    return " ".join(lines[:2]).strip()


def _expand_workflow_notice_query(question: str, tenant_id: str, agent: dict | None = None) -> str:
    """Broaden very short announcement-style questions so workflow KB retrieval can reach hotfix notice docs."""
    clean = str(question or "").strip()
    if not clean:
        return clean
    notice_terms = {"公告", "当前公告", "本周公告", "最新公告", "通知", "最新通知"}
    if clean not in notice_terms:
        return clean
    agent_name = str((agent or {}).get("name") or "").strip()
    notice_excerpt = _extract_hotfix_notice_excerpt(tenant_id, agent)
    extras = [agent_name, "本周公告", "当前公告", "通知", "具体内容", notice_excerpt]
    seen = set()
    tokens = []
    for item in [clean, *extras]:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        tokens.append(value)
    return " ".join(tokens)


def _resolve_agent_for_account(account: dict, requested_agent_id: str = "") -> dict | None:
    tenant_id = str(account.get("tenant_id") or "default").strip() or "default"
    if tenant_id == "default":
        return None
    clean_requested = str(requested_agent_id or "").strip()
    bound_ids = list_user_agent_bindings(tenant_id=tenant_id, phone=str(account.get("phone") or "").strip())
    if clean_requested:
        if bound_ids and clean_requested not in bound_ids:
            raise HTTPException(status_code=403, detail="当前账号无权访问该智能体")
        agent = get_agent(tenant_id, clean_requested)
        if not agent or not agent.get("enabled", True):
            raise HTTPException(status_code=404, detail="智能体不存在或已停用")
        return agent
    if bound_ids:
        for agent_id in bound_ids:
            agent = get_agent(tenant_id, agent_id)
            if agent and agent.get("enabled", True):
                return agent
        raise HTTPException(status_code=403, detail="当前账号未绑定可用智能体")
    return get_default_agent(tenant_id)


def _resolve_public_agent(tenant_id: str, agent_id: str) -> tuple[dict, dict]:
    clean_tenant_id = str(tenant_id or "").strip()
    clean_agent_id = str(agent_id or "").strip()
    if not clean_tenant_id or not clean_agent_id:
        raise HTTPException(status_code=400, detail="缺少 tenant_id 或 agent_id")
    tenant_row = next((item for item in list_tenants() if str(item.get("tenant_id") or "").strip() == clean_tenant_id), None)
    if not tenant_row:
        raise HTTPException(status_code=404, detail="租户不存在")
    agent = get_agent(clean_tenant_id, clean_agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="智能体不存在")
    if not agent.get("enabled", True):
        raise HTTPException(status_code=404, detail="智能体已停用")
    if str(agent.get("status") or "").strip() != "published":
        raise HTTPException(status_code=403, detail="智能体尚未发布")
    return tenant_row, agent


def _agent_public_payload(agent: dict | None) -> dict | None:
    if not agent:
        return None
    capability = _agent_model_capability(agent)
    return {
        "agent_id": agent.get("agent_id", ""),
        "name": agent.get("name", ""),
        "description": agent.get("description", ""),
        "avatar": agent.get("avatar", ""),
        "status": agent.get("status", "draft"),
        "enabled": bool(agent.get("enabled", True)),
        "is_default": bool(agent.get("is_default", False)),
        "workflow_id": agent.get("workflow_id", ""),
        "knowledge_scope": agent.get("knowledge_scope") or {},
        "tool_scope": agent.get("tool_scope") or [],
        "mcp_servers": agent.get("mcp_servers") or [],
        "recommended_questions": _normalize_public_questions(agent.get("recommended_questions")),
        "streaming": bool(agent.get("streaming", True)),
        "fallback_enabled": bool(agent.get("fallback_enabled", True)),
        "fallback_message": str(agent.get("fallback_message") or ""),
        "show_recommended": bool(agent.get("show_recommended", True)),
        "model": capability.get("model", ""),
        "supports_image": bool(capability.get("supports_image", False)),
        "capability_label": capability.get("capability_label", "文本"),
    }


def _mask_console_api_key(value: str) -> str:
    clean = str(value or "").strip()
    if len(clean) <= 10:
        return clean
    return f"{clean[:6]}{'*' * max(4, len(clean) - 10)}{clean[-4:]}"


def _extract_console_api_key(request: Request) -> str:
    auth = str(request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return str(request.headers.get("x-api-key") or "").strip()


def _resolve_console_api_agent(request: Request) -> tuple[dict, dict]:
    api_key = _extract_console_api_key(request)
    if not api_key:
        raise HTTPException(status_code=401, detail="缺少 API Key")
    agent = get_agent_by_publish_api_key(api_key)
    if not agent:
        raise HTTPException(status_code=401, detail="API Key 无效")
    tenant_id = str(agent.get("tenant_id") or "").strip()
    tenant = next((item for item in list_tenants() if str(item.get("tenant_id") or "").strip() == tenant_id), None)
    if not tenant:
        raise HTTPException(status_code=404, detail="租户不存在")
    if not agent.get("enabled", True):
        raise HTTPException(status_code=404, detail="智能体已停用")
    if str(agent.get("status") or "").strip() != "published":
        raise HTTPException(status_code=403, detail="智能体尚未发布")
    return tenant, agent


def _normalize_console_user_id(raw_user_id: str) -> str:
    clean = str(raw_user_id or "").strip() or "api_guest"
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in clean)
    return safe[:64] or "api_guest"


def _build_console_session_phone(user_id: str) -> str:
    return f"console::{_normalize_console_user_id(user_id)}"


def _build_console_api_response(workflow_result: dict, *, session_id: str) -> dict:
    if workflow_result.get("blocked"):
        raise HTTPException(status_code=400, detail=str(workflow_result.get("block_message") or "请求已被安全护栏拦截"))
    if workflow_result.get("cache_hit"):
        answer_text = str(workflow_result.get("cached_answer") or "").strip()
        knowledge_hits = build_knowledge_hits(workflow_result.get("cached_knowledge_hits"))
        retrieval_trace = dict(workflow_result.get("cached_retrieval_trace") or {})
    else:
        answer_text = str(workflow_result.get("answer_text") or "").strip()
        knowledge_hits = build_knowledge_hits(workflow_result.get("rag_results"))
        retrieval_trace = build_retrieval_trace(workflow_result, knowledge_hits)
    return {
        "ok": True,
        "session_id": session_id,
        "answer": answer_text,
        "knowledge_hits": knowledge_hits,
        "retrieval_trace": retrieval_trace,
        "model": str(workflow_result.get("selected_model") or ""),
    }


def _write_parsed_knowledge_file(*, tier_dir: str, parsed: ParsedKnowledgeFile) -> str:
    """把解析后的多格式文档统一落成 Markdown。"""
    os.makedirs(tier_dir, exist_ok=True)
    dest = os.path.join(tier_dir, parsed.filename)
    Path(dest).write_text(parsed.markdown_text, encoding="utf-8")
    return dest


def _human_readable_upload_result(*, parsed: ParsedKnowledgeFile, tier_label: str) -> str:
    """返回面向后台的上传结果说明。"""
    return (
        f"已上传并解析为 Markdown，目标层级【{tier_label}】；"
        f"来源格式：{parsed.source_type.upper()}；"
        f"解析文档段数：{parsed.document_count}；"
        f"预估知识片段：{parsed.chunk_count}"
    )

def _load_ingest_report() -> list[dict]:
    if not os.path.exists(INGEST_REPORT_PATH):
        return []
    try:
        with open(INGEST_REPORT_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _crawler_sources_with_status(tenant_id: str | None = None, tenant_name: str = "") -> list[dict]:
    """读取采集源并拼上最近执行状态。"""
    run_logs, _ = list_crawler_runs(page=1, per_page=200, tenant_id=tenant_id)
    latest_run_map: dict[str, dict] = {}
    for item in run_logs:
        source_id = str(item.get("source_id", "")).strip()
        if source_id and source_id not in latest_run_map:
            latest_run_map[source_id] = item
    items: list[dict] = []
    for source in load_crawler_sources(tenant_id=tenant_id, tenant_name=tenant_name):
        last = latest_run_map.get(source["source_id"], {})
        merged = dict(source)
        merged["last_status"] = str(last.get("status") or "never")
        merged["last_title"] = last.get("detail", "")
        merged["last_file"] = ""
        merged["last_items"] = int(last.get("items_count", 0) or 0)
        merged["last_error"] = last.get("detail", "") if merged["last_status"] == "failed" else ""
        merged["last_reason"] = ""
        merged["last_run_at"] = last.get("created_at", "")
        refresh_hours = int(merged.get("refresh_hours", 24) or 24)
        if merged["last_run_at"]:
            merged["next_run_hint"] = f"{refresh_hours} 小时后"
        else:
            merged["next_run_hint"] = "待首次执行"
        items.append(merged)
    return items

# --- Auth Endpoint ---
@app.post("/api/auth")
async def auth_endpoint(request: Request):
    body = await request.json()
    phone = body.get("phone", "").strip()
    password = body.get("password", "").strip()
    if not password or not phone:
        return JSONResponse({"ok": False, "msg": "缺少手机号或密码"}, status_code=400)
    result = verify_phone_login(phone, password)
    if not result["ok"]:
        return JSONResponse(result, status_code=result.get("code", 400))
    return JSONResponse(result)


@app.post("/api/password/change")
async def password_change_endpoint(request: Request):
    body = await request.json()
    phone = body.get("phone", "").strip()
    old_password = body.get("old_password", "").strip()
    new_password = body.get("new_password", "").strip()
    if not phone or not old_password or not new_password:
        return JSONResponse({"ok": False, "msg": "缺少手机号或密码"}, status_code=400)
    result = change_password(phone, old_password, new_password)
    if not result["ok"]:
        return JSONResponse(result, status_code=400)
    return result

# --- Balance Query Endpoint ---
@app.get("/api/balance")
async def balance_endpoint(request: Request):
    phone = request.query_params.get("phone", "").strip()
    if not phone:
        return JSONResponse({"ok": False, "msg": "缺少手机号"}, status_code=400)
    bal = check_balance(phone)
    if bal < 0:
        return JSONResponse({"ok": False, "msg": "手机号无效"}, status_code=401)
    return {"ok": True, "balance": bal}


@app.get("/api/public/app-config")
async def public_app_config():
    return {"ok": True, "config": get_public_app_config()}


@app.get("/api/public/tenant-app-config")
async def public_tenant_app_config(request: Request):
    phone = request.query_params.get("phone", "").strip()
    tenant_id = request.query_params.get("tenant_id", "").strip()
    agent_id = request.query_params.get("agent_id", "").strip()
    if tenant_id and agent_id:
        tenant, agent = _resolve_public_agent(tenant_id, agent_id)
        cfg = load_tenant_app_config(tenant["tenant_id"], tenant["tenant_name"])
        cfg = _apply_agent_overrides(cfg, agent)
        return {"ok": True, "config": build_public_app_config(cfg), "agent": _agent_public_payload(agent)}
    if not phone:
        return {"ok": True, "config": get_public_app_config()}
    account = get_phone_info(phone)
    if not account:
        return {"ok": True, "config": get_public_app_config()}
    tenant_id = str(account.get("tenant_id") or "default").strip() or "default"
    if tenant_id == "default":
        return {"ok": True, "config": get_public_app_config()}
    tenant_name = str(account.get("tenant_name") or account.get("display_name") or tenant_id).strip() or tenant_id
    cfg = load_tenant_app_config(tenant_id, tenant_name)
    agent = _resolve_agent_for_account(account, agent_id)
    cfg = _apply_agent_overrides(cfg, agent)
    return {"ok": True, "config": build_public_app_config(cfg), "agent": _agent_public_payload(agent)}


@app.post("/api/public/chat")
async def public_chat_endpoint(request: Request):
    body = await request.json()
    tenant_id = str(body.get("tenant_id", "") or "").strip()
    agent_id = str(body.get("agent_id", "") or "").strip()
    question = str(body.get("question", "") or "").strip()
    images = _normalize_chat_images(body.get("images"))
    if not question and not images:
        return JSONResponse({"ok": False, "msg": "请输入问题或上传图片"}, status_code=400)
    try:
        tenant, agent = _resolve_public_agent(tenant_id, agent_id)
    except HTTPException as exc:
        return JSONResponse({"ok": False, "msg": str(exc.detail)}, status_code=exc.status_code)
    request.state.ob_user_phone = "public_guest"
    request.state.ob_tenant_id = tenant["tenant_id"]
    runtime = _tenant_runtime_bundle(tenant)
    runtime["model_settings"] = _apply_agent_model_override(runtime["model_settings"], agent)
    if images and not bool(resolve_model_capability(runtime["model_settings"]).get("supports_image")):
        return JSONResponse({"ok": False, "msg": "当前智能体绑定的模型仅支持文本输入，请切换到支持图文的模型后再上传图片。"}, status_code=400)
    app_settings = _apply_agent_overrides(runtime["app_settings"], agent)
    app_settings["record_chat_logs"] = True
    active_agent_id = str(agent.get("agent_id") or "").strip()
    agent_workflow_id = str(agent.get("workflow_id") or "").strip()
    workflow_question = _expand_workflow_notice_query(question, tenant["tenant_id"], agent)
    workflow_cache_key = _tenant_cache_key(tenant["tenant_id"], question, active_agent_id)
    try:
        async with acquire_chat_slots(tenant_id=tenant["tenant_id"], agent_id=active_agent_id):
            if agent_workflow_id:
                workflow_result = _load_cached_chat_payload(workflow_cache_key, skip_cache_lookup=bool(images))
                if workflow_result is None:
                    try:
                        workflow_result = await execute_tenant_workflow(
                            tenant_id=tenant["tenant_id"],
                            tenant_name=tenant["tenant_name"],
                            workflow_id=agent_workflow_id,
                            input_payload={"text": workflow_question, "images": images, "agent_id": active_agent_id, "channel": "public_guest"},
                        )
                    except WorkflowRuntimeError as exc:
                        request.state.ob_error_message = str(exc)
                        _record_failed_workflow_chat_log(
                            request=request,
                            phone="public_guest",
                            question=question,
                            tenant_id=tenant["tenant_id"],
                            agent_id=active_agent_id,
                            workflow_id=agent_workflow_id,
                            error_message=str(exc),
                            status_code=400,
                            failure_stage="workflow_runtime",
                        )
                        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
                    except Exception as exc:
                        request.state.ob_error_message = f"workflow_failed: {exc}"
                        _record_failed_workflow_chat_log(
                            request=request,
                            phone="public_guest",
                            question=question,
                            tenant_id=tenant["tenant_id"],
                            agent_id=active_agent_id,
                            workflow_id=agent_workflow_id,
                            error_message=f"工作流执行失败：{exc}",
                            status_code=500,
                            failure_stage="workflow_exception",
                        )
                        return JSONResponse({"ok": False, "msg": f"工作流执行失败：{exc}"}, status_code=500)
                return _stream_workflow_chat_response(
                    request=request,
                    phone="public_guest",
                    question=question,
                    workflow_result=workflow_result,
                    tenant_id=tenant["tenant_id"],
                    agent_id=active_agent_id,
                    session_id="",
                    app_settings=app_settings,
                    cache_key=workflow_cache_key,
                    skip_cache_write=bool(images),
                )
            prompt_text = str((agent or {}).get("prompt_override") or "").strip() or runtime["prompt_template"]
            workflow_result = await run_chat_workflow_with_runtime(
                question=question,
                images=images,
                phone="public_guest",
                cache_key=_tenant_cache_key(tenant["tenant_id"], question, active_agent_id),
                tenant_id=tenant["tenant_id"],
                agent_id=active_agent_id,
                request_id=request.state.request_id,
                rag_runtime=runtime["rag_runtime"],
                app_loader=lambda: app_settings,
                model_loader=lambda: runtime["model_settings"],
                prompt_loader=lambda: prompt_text,
                llm_context={
                    "default_base_url": LLM_BASE_URL,
                    "ssl_ctx": _ssl_ctx,
                    "user_facing_error": user_facing_llm_error,
                },
                knowledge_scope=(agent or {}).get("knowledge_scope") or {},
                allowed_tools=(agent or {}).get("tool_scope") or [],
                mcp_servers=(agent or {}).get("mcp_servers") or [],
                log_chat=True,
                skip_cache_lookup=True,
                skip_cache_write=True,
            )
    except BusyError as exc:
        return _busy_json_response(request, exc)
    return _stream_chat_response(
        request=request,
        phone="public_guest",
        question=question,
        workflow_result=workflow_result,
        tenant_id=tenant["tenant_id"],
        agent_id=active_agent_id,
        session_id="",
    )


@app.post("/consoleapi/v1/chat")
async def console_api_chat(request: Request):
    body = await request.json()
    tenant, agent = _resolve_console_api_agent(request)
    question = str(body.get("message") or body.get("question") or "").strip()
    images = _normalize_chat_images(body.get("images"))
    session_id = str(body.get("session_id", "") or "").strip()
    stream = bool(body.get("stream", False))
    user_id = _normalize_console_user_id(str(body.get("user_id") or "api_guest"))
    phone = _build_console_session_phone(user_id)
    request.state.ob_user_phone = phone
    request.state.ob_tenant_id = tenant["tenant_id"]
    if not question and not images:
        return JSONResponse({"ok": False, "msg": "请输入消息内容或图片"}, status_code=400)
    if not session_id:
        session = create_chat_session(
            phone=phone,
            tenant_id=tenant["tenant_id"],
            agent_id=str(agent.get("agent_id") or "").strip(),
            title=question or "新对话",
        )
        session_id = str(session.get("session_id") or "").strip()
    runtime = _tenant_runtime_bundle(tenant)
    runtime["model_settings"] = _apply_agent_model_override(runtime["model_settings"], agent)
    if images and not bool(resolve_model_capability(runtime["model_settings"]).get("supports_image")):
        return JSONResponse({"ok": False, "msg": "当前智能体绑定的模型仅支持文本输入，请切换到支持图文的模型后再上传图片。"}, status_code=400)
    app_settings = _apply_agent_overrides(runtime["app_settings"], agent)
    app_settings["record_chat_logs"] = True
    active_agent_id = str(agent.get("agent_id") or "").strip()
    workflow_question = _expand_workflow_notice_query(question, tenant["tenant_id"], agent)
    agent_workflow_id = str(agent.get("workflow_id") or "").strip()
    workflow_cache_key = _tenant_cache_key(tenant["tenant_id"], question, active_agent_id)
    try:
        async with acquire_chat_slots(tenant_id=tenant["tenant_id"], agent_id=active_agent_id):
            if agent_workflow_id:
                workflow_result = _load_cached_chat_payload(workflow_cache_key, skip_cache_lookup=bool(images))
                if workflow_result is None:
                    try:
                        workflow_result = await execute_tenant_workflow(
                            tenant_id=tenant["tenant_id"],
                            tenant_name=tenant["tenant_name"],
                            workflow_id=agent_workflow_id,
                            input_payload={"text": workflow_question, "images": images, "session_id": session_id, "phone": phone, "agent_id": active_agent_id, "channel": "console_api"},
                        )
                    except WorkflowRuntimeError as exc:
                        request.state.ob_error_message = str(exc)
                        _record_failed_workflow_chat_log(
                            request=request,
                            phone=phone,
                            question=question,
                            tenant_id=tenant["tenant_id"],
                            agent_id=active_agent_id,
                            session_id=session_id,
                            workflow_id=agent_workflow_id,
                            error_message=str(exc),
                            status_code=400,
                            failure_stage="workflow_runtime",
                        )
                        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
                    except Exception as exc:
                        request.state.ob_error_message = f"workflow_failed: {exc}"
                        _record_failed_workflow_chat_log(
                            request=request,
                            phone=phone,
                            question=question,
                            tenant_id=tenant["tenant_id"],
                            agent_id=active_agent_id,
                            session_id=session_id,
                            workflow_id=agent_workflow_id,
                            error_message=f"工作流执行失败：{exc}",
                            status_code=500,
                            failure_stage="workflow_exception",
                        )
                        return JSONResponse({"ok": False, "msg": f"工作流执行失败：{exc}"}, status_code=500)
                if stream:
                    return _stream_workflow_chat_response(
                        request=request,
                        phone=phone,
                        question=question,
                        workflow_result=workflow_result,
                        tenant_id=tenant["tenant_id"],
                        agent_id=active_agent_id,
                        session_id=session_id,
                        app_settings=app_settings,
                        cache_key=workflow_cache_key,
                        skip_cache_write=bool(images),
                    )
                return _build_console_api_response(
                    {
                        **workflow_result,
                        "answer_text": _workflow_result_to_answer_text(workflow_result),
                    },
                    session_id=session_id,
                )
            prompt_text = str((agent or {}).get("prompt_override") or "").strip() or runtime["prompt_template"]
            workflow_result = await run_chat_workflow_with_runtime(
                question=question,
                images=images,
                phone=phone,
                cache_key=_tenant_cache_key(tenant["tenant_id"], question, active_agent_id),
                tenant_id=tenant["tenant_id"],
                agent_id=active_agent_id,
                session_id=session_id,
                request_id=request.state.request_id,
                rag_runtime=runtime["rag_runtime"],
                app_loader=lambda: app_settings,
                model_loader=lambda: runtime["model_settings"],
                prompt_loader=lambda: prompt_text,
                llm_context={
                    "default_base_url": LLM_BASE_URL,
                    "ssl_ctx": _ssl_ctx,
                    "user_facing_error": user_facing_llm_error,
                },
                knowledge_scope=(agent or {}).get("knowledge_scope") or {},
                allowed_tools=(agent or {}).get("tool_scope") or [],
                mcp_servers=(agent or {}).get("mcp_servers") or [],
                log_chat=True,
                skip_cache_lookup=bool(images),
                skip_cache_write=bool(images),
            )
    except BusyError as exc:
        return _busy_json_response(request, exc)
    if stream:
        return _stream_chat_response(
            request=request,
            phone=phone,
            question=question,
            workflow_result=workflow_result,
            tenant_id=tenant["tenant_id"],
            agent_id=active_agent_id,
            session_id=session_id,
        )
    return _build_console_api_response(workflow_result, session_id=session_id)


@app.get("/consoleapi/v1/conversations")
async def console_api_conversations(request: Request, user_id: str = "api_guest", limit: int = 20):
    tenant, agent = _resolve_console_api_agent(request)
    phone = _build_console_session_phone(user_id)
    return {
        "ok": True,
        "items": list_chat_sessions(
            phone=phone,
            tenant_id=tenant["tenant_id"],
            agent_id=str(agent.get("agent_id") or "").strip(),
            limit=max(1, min(int(limit or 20), 100)),
        ),
    }


@app.get("/consoleapi/v1/conversations/{session_id}/messages")
async def console_api_conversation_messages(request: Request, session_id: str, user_id: str = "api_guest", limit: int = 200):
    tenant, agent = _resolve_console_api_agent(request)
    phone = _build_console_session_phone(user_id)
    return {
        "ok": True,
        "items": list_chat_session_messages(
            session_id=session_id,
            phone=phone,
            tenant_id=tenant["tenant_id"],
            agent_id=str(agent.get("agent_id") or "").strip(),
            limit=max(1, min(int(limit or 200), 500)),
        ),
    }


@app.post("/api/mock/hospital-mcp")
async def mock_hospital_mcp_bridge(request: Request):
    """本地调试用 MCP Bridge。"""
    if mock_hospital_mcp_result is None:
        raise HTTPException(status_code=404, detail="本地调试桥接未启用")
    body = await request.json()
    tool_name = str(body.get("tool") or "").strip()
    payload = body.get("input")
    if not tool_name:
        return JSONResponse({"ok": False, "message": "缺少 tool"}, status_code=400)
    return {
        "ok": True,
        "message": "local mcp bridge",
        "result": mock_hospital_mcp_result(tool_name, payload),
    }


def _verify_front_chat_user(phone: str, password: str, uuid_value: str = "", requested_agent_id: str = "") -> tuple[dict, dict, dict | None]:
    """校验前台用户并返回账号信息，供聊天会话接口复用。"""
    auth_result = verify_phone_login(phone, password)
    if not auth_result["ok"]:
        raise HTTPException(status_code=auth_result.get("code", 403), detail=auth_result.get("msg", "未授权"))
    if auth_result.get("must_change_password"):
        raise HTTPException(status_code=403, detail="请先修改临时密码后再开始提问")
    account = get_phone_info(phone) or {}
    tenant_id = str(account.get("tenant_id") or "default").strip() or "default"
    tenant_name = str(account.get("tenant_name") or account.get("display_name") or tenant_id).strip() or tenant_id
    account_payload = {
        **account,
        "tenant_id": tenant_id,
        "tenant_name": tenant_name,
    }
    agent = _resolve_agent_for_account(account_payload, requested_agent_id)
    return auth_result, account_payload, agent


def _build_cached_stream(
    *,
    request: Request,
    phone: str,
    cached_text: str,
    tenant_id: str,
    agent_id: str = "",
    session_id: str = "",
    knowledge_hits: list[dict] | None = None,
    retrieval_trace: dict | None = None,
):
    """输出缓存命中的流式响应。"""

    async def cached_stream():
        if knowledge_hits is not None or retrieval_trace is not None:
            yield f"data: {json.dumps({'session_id': session_id, 'knowledge_hits': knowledge_hits or [], 'retrieval_trace': retrieval_trace or {}}, ensure_ascii=False)}\n\n"
        payload = {"status": "cache_hit", "content": ""}
        yield f"data: {json.dumps(payload)}\n\n"
        for i in range(0, len(cached_text), 10):
            chunk, output_events = apply_output_guardrails(cached_text[i:i + 10])
            for event in output_events:
                record_guardrail_event(
                    request_id=request.state.request_id,
                    phone=phone,
                    tenant_id=tenant_id,
                    stage=event.get("stage", "output"),
                    action=event.get("action", "mask"),
                    rule_name=event.get("rule", "unknown"),
                    detail=event.get("detail", ""),
                )
            yield f"data: {json.dumps({'content': chunk})}\n\n"
            await asyncio.sleep(0.02)
        yield "data: [DONE]\n\n"

    return StreamingResponse(cached_stream(), media_type="text/event-stream")


def _stream_chat_response(
    *,
    request: Request,
    phone: str,
    question: str,
    workflow_result: dict,
    tenant_id: str = "default",
    agent_id: str = "",
    session_id: str = "",
):
    """把问答结果统一流式输出。

    平台问答和租户问答共用这一条输出链，避免两套 SSE 逻辑越改越散。
    """
    request.state.ob_orchestration = workflow_result.get("orchestration_backend", "")
    for event in workflow_result.get("guardrail_events", []):
        record_guardrail_event(
            request_id=request.state.request_id,
            phone=phone,
            tenant_id=tenant_id,
            stage=event.get("stage", "input"),
            action=event.get("action", "block"),
            rule_name=event.get("rule", "unknown"),
            detail=event.get("detail", ""),
        )
    if workflow_result.get("blocked"):
        request.state.ob_error_message = workflow_result.get("block_message", "blocked")
        return JSONResponse(
            {"ok": False, "msg": workflow_result.get("block_message", "请求已被安全护栏拦截")},
            status_code=400,
        )

    question = workflow_result.get("question", question)
    cached = workflow_result.get("cached_answer", "") if workflow_result.get("cache_hit") else ""
    if cached:
        request.state.ob_cache_status = "hit"
        cached_hits = list(workflow_result.get("cached_knowledge_hits") or [])
        cached_trace = dict(workflow_result.get("cached_retrieval_trace") or {})
        cached_trace["phase_timings"] = {
            **dict(cached_trace.get("phase_timings") or {}),
            **dict(workflow_result.get("phase_timings") or {}),
            "cache_hit": True,
        }
        if (workflow_result.get("app_settings") or {}).get("record_chat_logs", True) and workflow_result.get("log_chat", True):
            record_chat_log(
                phone=phone,
                question=question,
                answer=cached,
                knowledge_hits=cached_hits,
                retrieval_trace=cached_trace,
                tenant_id=tenant_id,
                agent_id=agent_id,
                request_id=request.state.request_id,
                session_id=session_id,
            )
        return _build_cached_stream(
            request=request,
            phone=phone,
            cached_text=cached,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            knowledge_hits=cached_hits,
            retrieval_trace=cached_trace,
        )

    answer_events = workflow_result.get("answer_events") or []
    if workflow_result.get("selected_model"):
        request.state.ob_model_name = workflow_result.get("selected_model", "")
    if workflow_result.get("llm_error_message"):
        request.state.ob_error_message = workflow_result.get("llm_error_message", "")

    knowledge_hits = build_knowledge_hits(workflow_result.get("rag_results"))
    retrieval_trace = build_retrieval_trace(workflow_result, knowledge_hits)

    async def llm_stream():
        # 对于租户问答测试，始终发送检索结果（即使为空），确保前端能显示"本次未命中"
        yield f"data: {json.dumps({'session_id': session_id, 'knowledge_hits': knowledge_hits, 'retrieval_trace': retrieval_trace}, ensure_ascii=False)}\n\n"
        for line in answer_events:
            yield line
        if not answer_events or answer_events[-1].strip() != "data: [DONE]":
            yield "data: [DONE]\n\n"

    async def queued_llm_stream():
        global _active_llm_calls
        if _active_llm_calls >= _MAX_CONCURRENT_LLM:
            pos = _active_llm_calls - _MAX_CONCURRENT_LLM + 1
            yield f"data: {json.dumps({'status': 'queued', 'position': pos, 'content': ''})}\n\n"
            while _active_llm_calls >= _MAX_CONCURRENT_LLM:
                await asyncio.sleep(0.5)
                pos = max(1, _active_llm_calls - _MAX_CONCURRENT_LLM + 1)
                yield f"data: {json.dumps({'status': 'queued', 'position': pos, 'content': ''})}\n\n"

        _active_llm_calls += 1
        yield f"data: {json.dumps({'status': 'generating', 'content': ''})}\n\n"
        try:
            async for chunk in llm_stream():
                yield chunk
        finally:
            _active_llm_calls -= 1

    return StreamingResponse(queued_llm_stream(), media_type="text/event-stream")


def _tenant_cache_key(tenant_id: str, question: str, agent_id: str = "") -> str:
    """生成租户隔离后的语义缓存键。"""
    clean_tenant_id = str(tenant_id or "default").strip() or "default"
    clean_agent_id = str(agent_id or "").strip() or "_default"
    return f"tenant::{clean_tenant_id}::agent::{clean_agent_id}::{question.strip()}"


def _load_cached_chat_payload(cache_key: str, *, skip_cache_lookup: bool = False) -> dict | None:
    """从语义缓存中读取聊天结果，并归一成工作流/问答入口可复用的结构。"""
    if skip_cache_lookup:
        return None
    clean_cache_key = str(cache_key or "").strip()
    if not clean_cache_key:
        return None
    cached = semantic_cache.get(clean_cache_key)
    if not isinstance(cached, dict):
        return None
    answer_text = str(cached.get("answer") or "").strip()
    if not answer_text:
        return None
    return {
        "cache_hit": True,
        "cached_answer": answer_text,
        "cached_knowledge_hits": list(cached.get("knowledge_hits") or []),
        "cached_retrieval_trace": dict(cached.get("retrieval_trace") or {}),
    }

# --- Chat SSE Endpoint ---
@app.post("/api/chat")
async def chat_endpoint(request: Request):
    body = await request.json()
    phone = body.get("phone", "").strip()
    password = body.get("password", "").strip()
    question = body.get("question", "").strip()
    images = _normalize_chat_images(body.get("images"))
    session_id = str(body.get("session_id", "") or "").strip()
    agent_id = str(body.get("agent_id", "") or "").strip()

    if not password or not phone:
        return JSONResponse({"ok": False, "msg": "未授权"}, status_code=401)

    # Verify auth
    try:
        auth_result, account, agent = _verify_front_chat_user(phone, password, requested_agent_id=agent_id)
    except HTTPException as exc:
        request.state.ob_error_message = str(exc.detail)
        return JSONResponse({"ok": False, "msg": str(exc.detail)}, status_code=exc.status_code)
    request.state.ob_user_phone = phone
    tenant_id = str(account.get("tenant_id") or "default").strip() or "default"
    tenant_name = str(account.get("tenant_name") or account.get("display_name") or tenant_id).strip() or tenant_id
    request.state.ob_tenant_id = tenant_id
    active_agent_id = str((agent or {}).get("agent_id") or "").strip()
    workflow_question = _expand_workflow_notice_query(question, tenant_id, agent)

    # Rate limit
    if not check_rate_limit(phone):
        request.state.ob_error_message = "rate_limited"
        return JSONResponse({"ok": False, "msg": "请求过于频繁，请稍后再试"}, status_code=429)

    if not question and not images:
        request.state.ob_error_message = "empty_question"
        return JSONResponse({"ok": False, "msg": "请输入问题或上传图片"}, status_code=400)

    if not session_id:
        session = create_chat_session(
            phone=phone,
            tenant_id=tenant_id,
            agent_id=active_agent_id,
            title=question or "新对话",
        )
        session_id = str(session.get("session_id") or "").strip()

    llm_context = {
        "default_base_url": LLM_BASE_URL,
        "ssl_ctx": _ssl_ctx,
        "user_facing_error": user_facing_llm_error,
    }
    if tenant_id != "default":
        runtime = _tenant_runtime_bundle({"tenant_id": tenant_id, "tenant_name": tenant_name})
        runtime["model_settings"] = _apply_agent_model_override(runtime["model_settings"], agent)
        if images and not bool(resolve_model_capability(runtime["model_settings"]).get("supports_image")):
            return JSONResponse({"ok": False, "msg": "当前智能体绑定的模型仅支持文本输入，请切换到支持图文的模型后再上传图片。"}, status_code=400)
        app_settings = _apply_agent_overrides(runtime["app_settings"], agent)
        agent_workflow_id = str((agent or {}).get("workflow_id") or "").strip()
        workflow_cache_key = _tenant_cache_key(tenant_id, question, active_agent_id)
        try:
            async with acquire_chat_slots(tenant_id=tenant_id, agent_id=active_agent_id):
                if agent_workflow_id:
                    workflow_result = _load_cached_chat_payload(workflow_cache_key, skip_cache_lookup=bool(images))
                    if workflow_result is None:
                        try:
                            workflow_result = await execute_tenant_workflow(
                                tenant_id=tenant_id,
                                tenant_name=tenant_name,
                                workflow_id=agent_workflow_id,
                                input_payload={"text": workflow_question, "images": images, "session_id": session_id, "phone": phone, "agent_id": active_agent_id},
                            )
                        except WorkflowRuntimeError as exc:
                            request.state.ob_error_message = str(exc)
                            _record_failed_workflow_chat_log(
                                request=request,
                                phone=phone,
                                question=question,
                                tenant_id=tenant_id,
                                agent_id=active_agent_id,
                                session_id=session_id,
                                workflow_id=agent_workflow_id,
                                error_message=str(exc),
                                status_code=400,
                                failure_stage="workflow_runtime",
                            )
                            return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
                        except Exception as exc:
                            request.state.ob_error_message = f"workflow_failed: {exc}"
                            _record_failed_workflow_chat_log(
                                request=request,
                                phone=phone,
                                question=question,
                                tenant_id=tenant_id,
                                agent_id=active_agent_id,
                                session_id=session_id,
                                workflow_id=agent_workflow_id,
                                error_message=f"工作流执行失败：{exc}",
                                status_code=500,
                                failure_stage="workflow_exception",
                            )
                            return JSONResponse({"ok": False, "msg": f"工作流执行失败：{exc}"}, status_code=500)
                    return _stream_workflow_chat_response(
                        request=request,
                        phone=phone,
                        question=question,
                        workflow_result=workflow_result,
                        tenant_id=tenant_id,
                        agent_id=active_agent_id,
                        session_id=session_id,
                        app_settings=app_settings,
                        cache_key=workflow_cache_key,
                        skip_cache_write=bool(images),
                    )
                prompt_text = str((agent or {}).get("prompt_override") or "").strip() or runtime["prompt_template"]
                workflow_result = await run_chat_workflow_with_runtime(
                    question=question,
                    images=images,
                    phone=phone,
                    cache_key=_tenant_cache_key(tenant_id, question, active_agent_id),
                    tenant_id=tenant_id,
                    agent_id=active_agent_id,
                    session_id=session_id,
                    request_id=request.state.request_id,
                    rag_runtime=runtime["rag_runtime"],
                    app_loader=lambda: app_settings,
                    model_loader=lambda: runtime["model_settings"],
                    prompt_loader=lambda: prompt_text,
                    llm_context=llm_context,
                    knowledge_scope=(agent or {}).get("knowledge_scope") or {},
                    allowed_tools=(agent or {}).get("tool_scope") or [],
                    mcp_servers=(agent or {}).get("mcp_servers") or [],
                    skip_cache_lookup=bool(images),
                    skip_cache_write=bool(images),
                )
        except BusyError as exc:
            return _busy_json_response(request, exc)
    else:
        if images and not bool(resolve_model_capability(load_model_config()).get("supports_image")):
            return JSONResponse({"ok": False, "msg": "当前系统默认模型仅支持文本输入，请切换到支持图文的模型后再上传图片。"}, status_code=400)
        try:
            async with acquire_chat_slots(tenant_id="default", agent_id=active_agent_id):
                workflow_result = await run_chat_workflow(
                    question=question,
                    images=images,
                    phone=phone,
                    cache_key=question,
                    tenant_id="default",
                    agent_id=active_agent_id,
                    session_id=session_id,
                    request_id=request.state.request_id,
                    llm_context=llm_context,
                    skip_cache_lookup=bool(images),
                    skip_cache_write=bool(images),
                )
        except BusyError as exc:
            return _busy_json_response(request, exc)
    return _stream_chat_response(
        request=request,
        phone=phone,
        question=question,
        workflow_result=workflow_result,
        tenant_id=tenant_id,
        agent_id=active_agent_id,
        session_id=session_id,
    )


@app.post("/api/chat/sessions/list")
async def chat_sessions_list(request: Request):
    body = await request.json()
    phone = str(body.get("phone", "") or "").strip()
    password = str(body.get("password", "") or "").strip()
    agent_id = str(body.get("agent_id", "") or "").strip()
    if not phone or not password:
        return JSONResponse({"ok": False, "msg": "未授权"}, status_code=401)
    try:
        _, account, agent = _verify_front_chat_user(phone, password, requested_agent_id=agent_id)
    except HTTPException as exc:
        return JSONResponse({"ok": False, "msg": str(exc.detail)}, status_code=exc.status_code)
    cleanup_empty_chat_sessions(phone=phone, tenant_id=account["tenant_id"], keep_latest=1)
    sessions = list_chat_sessions(
        phone=phone,
        tenant_id=account["tenant_id"],
        agent_id=str((agent or {}).get("agent_id") or "").strip() if agent else None,
    )
    return {"ok": True, "sessions": sessions, "agent": _agent_public_payload(agent)}


@app.post("/api/chat/sessions/create")
async def chat_sessions_create(request: Request):
    body = await request.json()
    phone = str(body.get("phone", "") or "").strip()
    password = str(body.get("password", "") or "").strip()
    title = str(body.get("title", "") or "").strip()
    agent_id = str(body.get("agent_id", "") or "").strip()
    if not phone or not password:
        return JSONResponse({"ok": False, "msg": "未授权"}, status_code=401)
    try:
        _, account, agent = _verify_front_chat_user(phone, password, requested_agent_id=agent_id)
    except HTTPException as exc:
        return JSONResponse({"ok": False, "msg": str(exc.detail)}, status_code=exc.status_code)
    session = create_chat_session(
        phone=phone,
        tenant_id=account["tenant_id"],
        agent_id=str((agent or {}).get("agent_id") or "").strip(),
        title=title or "新对话",
    )
    return {"ok": True, "session": session, "agent": _agent_public_payload(agent)}


@app.post("/api/chat/sessions/messages")
async def chat_sessions_messages(request: Request):
    body = await request.json()
    phone = str(body.get("phone", "") or "").strip()
    password = str(body.get("password", "") or "").strip()
    session_id = str(body.get("session_id", "") or "").strip()
    agent_id = str(body.get("agent_id", "") or "").strip()
    if not phone or not password:
        return JSONResponse({"ok": False, "msg": "未授权"}, status_code=401)
    if not session_id:
        return JSONResponse({"ok": False, "msg": "缺少会话ID"}, status_code=400)
    try:
        _, account, agent = _verify_front_chat_user(phone, password, requested_agent_id=agent_id)
    except HTTPException as exc:
        return JSONResponse({"ok": False, "msg": str(exc.detail)}, status_code=exc.status_code)
    messages = list_chat_session_messages(
        session_id=session_id,
        phone=phone,
        tenant_id=account["tenant_id"],
        agent_id=str((agent or {}).get("agent_id") or "").strip() if agent else None,
    )
    return {"ok": True, "messages": messages, "agent": _agent_public_payload(agent)}


@app.post("/api/chat/agents")
async def front_chat_agents(request: Request):
    """前台：返回当前账号可访问的智能体列表，便于做独立入口和切换器。"""
    body = await request.json()
    phone = str(body.get("phone", "") or "").strip()
    password = str(body.get("password", "") or "").strip()
    if not phone or not password:
        return JSONResponse({"ok": False, "msg": "未授权"}, status_code=401)
    try:
        _, account, active_agent = _verify_front_chat_user(phone, password, requested_agent_id=str(body.get("agent_id", "") or "").strip())
    except HTTPException as exc:
        return JSONResponse({"ok": False, "msg": str(exc.detail)}, status_code=exc.status_code)
    tenant_id = str(account.get("tenant_id") or "default").strip() or "default"
    bound_ids = list_user_agent_bindings(tenant_id=tenant_id, phone=phone)
    items = list_agents(tenant_id, include_disabled=False)
    if bound_ids:
        allowed = set(bound_ids)
        items = [item for item in items if item.get("agent_id") in allowed]
    return {
        "ok": True,
        "items": [_agent_public_payload(item) for item in items],
        "active_agent": _agent_public_payload(active_agent),
    }

# --- Queue Status Endpoint ---
@app.get("/api/queue/status")
async def queue_status():
    return {
        "active": _active_llm_calls,
        "max": _MAX_CONCURRENT_LLM,
        "queued": max(0, _active_llm_calls - _MAX_CONCURRENT_LLM),
    }

# --- Admin Endpoints ---
@app.post("/api/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    if body.get("username") == ADMIN_USERNAME and body.get("password") == ADMIN_PASSWORD:
        import base64
        token = base64.b64encode(f"{ADMIN_USERNAME}:{ADMIN_PASSWORD}".encode()).decode()
        return {"ok": True, "token": token}
    return JSONResponse({"ok": False, "msg": "账号或密码错误"}, status_code=401)

@app.get("/api/admin/accounts")
async def admin_list_accounts(request: Request, page: int = 1):
    verify_admin(request)
    accounts, total = list_phone_accounts(page)
    return {"ok": True, "accounts": accounts, "total": total, "page": page}


@app.get("/api/admin/chat-logs")
async def admin_chat_logs(request: Request, page: int = 1, phone: str = ""):
    verify_admin(request)
    logs, total = list_chat_logs(page=page, per_page=20, phone=phone)
    return {"ok": True, "logs": logs, "total": total, "page": page}


@app.get("/api/admin/observability/summary")
async def admin_observability_summary(request: Request):
    verify_admin(request)
    rag_stats = rag_engine.get_stats()
    return {
        "ok": True,
        "summary": {
            **get_observability_summary(),
            "retrieval": {
                "backend": rag_stats.get("retrieval_backend", "local_tfidf"),
                "dense": (rag_stats.get("retrieval_backends") or {}).get("dense", {}),
                "rerank": rag_stats.get("rerank", {}),
            },
        },
        "cache": semantic_cache.health(),
    }


@app.get("/api/admin/evaluations")
async def admin_evaluations(request: Request, page: int = 1):
    """平台总后台：查看检索评测历史。"""
    verify_admin(request)
    logs, total = list_evaluation_runs(page=page, per_page=20)
    return {"ok": True, "logs": logs, "total": total, "page": page}


@app.post("/api/admin/evaluations/run")
async def admin_run_evaluations(request: Request):
    """平台总后台：运行一轮检索评测。"""
    verify_admin(request)
    body = await request.json()
    cases = body.get("cases") or []
    if not isinstance(cases, list) or not cases:
        return JSONResponse({"ok": False, "msg": "请提供评测题集 cases"}, status_code=400)
    name = str(body.get("name") or "平台检索评测").strip() or "平台检索评测"
    backend_override = str(body.get("backend_override") or "").strip() or None
    retrieval_settings = load_retrieval_config()
    result = await run_retrieval_evaluation(
        rag_runtime=rag_engine,
        cases=cases,
        retrieval_config=retrieval_settings,
        backend_override=backend_override,
        tenant_id="default",
        app_loader=load_app_config,
        model_loader=load_model_config,
        prompt_loader=load_system_prompt_template,
        llm_context={
            "default_base_url": LLM_BASE_URL,
            "ssl_ctx": _ssl_ctx,
            "user_facing_error": user_facing_llm_error,
        },
    )
    record_evaluation_run(
        tenant_id="default",
        name=name,
        total_questions=result["total_questions"],
        hit_at_1=result["hit_at_1"],
        hit_at_3=result["hit_at_3"],
        hit_at_5=result["hit_at_5"],
        avg_top_score=result["avg_top_score"],
        detail=result.get("detail") or [],
        config_snapshot=result.get("config_snapshot") or {},
    )
    return {"ok": True, "result": result}


@app.get("/api/admin/observability/requests")
async def admin_request_logs(request: Request, page: int = 1):
    verify_admin(request)
    logs, total = list_request_logs(page=page, per_page=20)
    return {"ok": True, "logs": logs, "total": total, "page": page}


@app.get("/api/admin/guardrail-events")
async def admin_guardrail_events(request: Request, page: int = 1):
    verify_admin(request)
    logs, total = list_guardrail_events(page=page, per_page=20)
    return {"ok": True, "logs": logs, "total": total, "page": page}


@app.get("/api/admin/bindings")
async def admin_list_bindings(request: Request, page: int = 1):
    verify_admin(request)
    accounts, total = list_phone_accounts(page)
    groups = [
        {
            "phone": account["phone"],
            "key_count": 1,
            "total_balance": account["balance"] or 0,
            "keys": [account],
        }
        for account in accounts
    ]
    return {"ok": True, "groups": groups, "total": total, "page": page}

@app.post("/api/admin/phone/reset_devices")
async def admin_reset_phone_devices(request: Request):
    verify_admin(request)
    body = await request.json()
    phone = body.get("phone", "").strip()
    if not phone:
        return JSONResponse({"ok": False, "msg": "请提供手机号"}, status_code=400)
    success = reset_device_binding(phone)
    if success:
        return {"ok": True, "msg": f"手机号 {phone} 的设备绑定已清空"}
    return JSONResponse({"ok": False, "msg": "手机号不存在"}, status_code=404)

@app.post("/api/admin/phone/generate_password")
async def admin_generate_phone_password(request: Request):
    verify_admin(request)
    body = await request.json()
    phone = body.get("phone", "").strip()
    if not phone:
        return JSONResponse({"ok": False, "msg": "请输入手机号"}, status_code=400)
    return generate_temp_password(phone)

@app.get("/api/admin/phone/account")
async def admin_search_account(request: Request, phone: str = ""):
    verify_admin(request)
    info = get_phone_info(phone.strip())
    if info:
        return {"ok": True, "account": info}
    return JSONResponse({"ok": False, "msg": "未找到该手机号账号"}, status_code=404)

@app.post("/api/admin/topup")
async def admin_topup(request: Request):
    """Admin: add balance to a phone number."""
    verify_admin(request)
    body = await request.json()
    phone = body.get("phone", "").strip()
    amount = int(body.get("amount", 500))
    if not phone:
        return JSONResponse({"ok": False, "msg": "请输入手机号"}, status_code=400)
    if amount <= 0 or amount > 99999:
        return JSONResponse({"ok": False, "msg": "充值点数无效（1-99999）"}, status_code=400)
    result = add_balance_by_phone(phone, amount)
    if not result["ok"]:
        return JSONResponse(result, status_code=404)
    return result

@app.get("/api/admin/phone/search")
async def admin_search_phone(request: Request, phone: str = ""):
    """Admin: search phone account."""
    verify_admin(request)
    phone = phone.strip()
    if not phone:
        return JSONResponse({"ok": False, "msg": "请输入手机号"}, status_code=400)
    accounts = search_by_phone(phone)
    if accounts:
        return {"ok": True, "accounts": accounts, "count": len(accounts)}
    return JSONResponse({"ok": False, "msg": f"未找到手机号 {phone} 的账号记录"}, status_code=404)


@app.get("/api/admin/tenants")
async def admin_list_tenants(request: Request):
    """平台总后台：列出租户。"""
    verify_admin(request)
    return {"ok": True, "items": list_tenants()}


@app.post("/api/admin/tenants")
async def admin_create_tenant(request: Request):
    """平台总后台：创建租户。"""
    verify_admin(request)
    body = await request.json()
    try:
        tenant = create_tenant(
            tenant_id=body.get("tenant_id", ""),
            tenant_name=body.get("tenant_name", ""),
            admin_username=body.get("admin_username", ""),
            admin_password=body.get("admin_password", ""),
        )
        ensure_tenant_storage(tenant["tenant_id"], tenant["tenant_name"])
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {"ok": True, "msg": "租户创建成功", "tenant": tenant, "items": list_tenants()}


@app.put("/api/admin/tenants")
async def admin_update_tenant(request: Request):
    """平台总后台：更新租户信息。"""
    verify_admin(request)
    body = await request.json()
    try:
        tenant = update_tenant(
            tenant_id=body.get("tenant_id", ""),
            tenant_name=body.get("tenant_name", ""),
            admin_username=body.get("admin_username", ""),
            enabled=bool(body.get("enabled", True)),
            admin_password=body.get("admin_password", ""),
        )
        ensure_tenant_storage(tenant["tenant_id"], tenant["tenant_name"])
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {"ok": True, "msg": "租户已更新", "tenant": tenant, "items": list_tenants()}


@app.post("/api/tenant/auth")
async def tenant_auth_endpoint(request: Request):
    """租户后台登录验证。"""
    body = await request.json()
    result = verify_tenant_admin(body.get("username", ""), body.get("password", ""))
    if not result.get("ok"):
        return JSONResponse(result, status_code=401)
    ensure_tenant_storage(result["tenant_id"], result["tenant_name"])
    return result


@app.post("/api/tenant/chat")
async def tenant_chat_endpoint(request: Request):
    """租户后台：用本租户配置直接发起问答。

    这条链路会真实使用租户自己的 Prompt、模型配置、检索配置和知识空间，
    不再复用平台默认问答运行时。
    """
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    question = str(body.get("question", "") or "").strip()
    images = _normalize_chat_images(body.get("images"))
    operator = str(body.get("operator", "") or "").strip() or tenant["admin_username"]
    agent_id = str(body.get("agent_id", "") or "").strip()
    request.state.ob_user_phone = operator
    request.state.ob_tenant_id = tenant["tenant_id"]

    if not question and not images:
        request.state.ob_error_message = "empty_question"
        return JSONResponse({"ok": False, "msg": "请输入问题或上传图片"}, status_code=400)

    agent = get_agent(tenant["tenant_id"], agent_id) if agent_id else None
    if agent_id and not agent:
        return JSONResponse({"ok": False, "msg": "智能体不存在"}, status_code=404)
    if agent and not agent.get("enabled", True):
        return JSONResponse({"ok": False, "msg": "智能体已停用"}, status_code=400)
    runtime = _tenant_runtime_bundle(tenant)
    runtime["model_settings"] = _apply_agent_model_override(runtime["model_settings"], agent)
    if images and not bool(resolve_model_capability(runtime["model_settings"]).get("supports_image")):
        return JSONResponse({"ok": False, "msg": "当前智能体绑定的模型仅支持文本输入，请切换到支持图文的模型后再上传图片。"}, status_code=400)
    app_settings = _apply_agent_overrides(runtime["app_settings"], agent)
    agent_workflow_id = str((agent or {}).get("workflow_id") or "").strip()
    active_agent_id = str((agent or {}).get("agent_id") or "").strip()
    workflow_question = _expand_workflow_notice_query(question, tenant["tenant_id"], agent)
    workflow_cache_key = _tenant_cache_key(tenant["tenant_id"], question, active_agent_id)
    try:
        async with acquire_chat_slots(tenant_id=tenant["tenant_id"], agent_id=active_agent_id):
            if agent_workflow_id:
                workflow_result = _load_cached_chat_payload(workflow_cache_key, skip_cache_lookup=bool(images))
                if workflow_result is None:
                    try:
                        workflow_result = await execute_tenant_workflow(
                            tenant_id=tenant["tenant_id"],
                            tenant_name=tenant["tenant_name"],
                            workflow_id=agent_workflow_id,
                            input_payload={"text": workflow_question, "images": images, "operator": operator, "agent_id": active_agent_id},
                        )
                    except WorkflowRuntimeError as exc:
                        request.state.ob_error_message = str(exc)
                        _record_failed_workflow_chat_log(
                            request=request,
                            phone=operator,
                            question=question,
                            tenant_id=tenant["tenant_id"],
                            agent_id=active_agent_id,
                            workflow_id=agent_workflow_id,
                            error_message=str(exc),
                            status_code=400,
                            failure_stage="workflow_runtime",
                        )
                        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
                    except Exception as exc:
                        request.state.ob_error_message = f"workflow_failed: {exc}"
                        _record_failed_workflow_chat_log(
                            request=request,
                            phone=operator,
                            question=question,
                            tenant_id=tenant["tenant_id"],
                            agent_id=active_agent_id,
                            workflow_id=agent_workflow_id,
                            error_message=f"工作流执行失败：{exc}",
                            status_code=500,
                            failure_stage="workflow_exception",
                        )
                        return JSONResponse({"ok": False, "msg": f"工作流执行失败：{exc}"}, status_code=500)
                return _stream_workflow_chat_response(
                    request=request,
                    phone=operator,
                    question=question,
                    workflow_result=workflow_result,
                    tenant_id=tenant["tenant_id"],
                    agent_id=active_agent_id,
                    app_settings=app_settings,
                    cache_key=workflow_cache_key,
                    skip_cache_write=bool(images),
                )
            prompt_text = str((agent or {}).get("prompt_override") or "").strip() or runtime["prompt_template"]
            workflow_result = await run_chat_workflow_with_runtime(
                question=question,
                images=images,
                phone=operator,
                cache_key=_tenant_cache_key(tenant["tenant_id"], question, active_agent_id),
                tenant_id=tenant["tenant_id"],
                agent_id=active_agent_id,
                request_id=request.state.request_id,
                rag_runtime=runtime["rag_runtime"],
                app_loader=lambda: app_settings,
                model_loader=lambda: runtime["model_settings"],
                prompt_loader=lambda: prompt_text,
                llm_context={
                    "default_base_url": LLM_BASE_URL,
                    "ssl_ctx": _ssl_ctx,
                    "user_facing_error": user_facing_llm_error,
                },
                knowledge_scope=(agent or {}).get("knowledge_scope") or {},
                allowed_tools=(agent or {}).get("tool_scope") or [],
                mcp_servers=(agent or {}).get("mcp_servers") or [],
                skip_cache_lookup=bool(images),
                skip_cache_write=bool(images),
            )
    except BusyError as exc:
        return _busy_json_response(request, exc)
    return _stream_chat_response(
        request=request,
        phone=operator,
        question=question,
        workflow_result=workflow_result,
        tenant_id=tenant["tenant_id"],
        agent_id=active_agent_id,
    )

@app.post("/api/admin/knowledge/upload")
async def admin_upload_knowledge(request: Request, file: UploadFile = File(...)):
    verify_admin(request)
    content = await file.read()
    try:
        parsed = parse_uploaded_knowledge_file(
            filename=file.filename or "uploaded.md",
            raw_bytes=content,
            tier="permanent",
            temp_dir=Path(_knowledge_dir()) / ".upload_tmp" / "admin",
        )
    except UnsupportedDocumentError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "msg": f"文档解析失败：{exc}"}, status_code=500)

    target_dir = _knowledge_storage_dir(_knowledge_dir(), "kb_default", "uncategorized")
    _write_parsed_knowledge_file(tier_dir=target_dir, parsed=parsed)
    count = rag_engine.build_index()
    semantic_cache.clear()
    return {
        "ok": True,
        "msg": f"已上传并解析为 Markdown，已按知识库目录归档；共生成 {parsed.document_count} 个文档、{parsed.chunk_count} 个检索切片。",
        "chunks": count,
        "tier": "knowledge",
        "stored_file": parsed.filename,
        "source_type": parsed.source_type,
    }

@app.get("/api/admin/knowledge/stats")
async def admin_knowledge_stats(request: Request):
    verify_admin(request)
    stats = rag_engine.get_stats()
    stats["files"] = rag_engine.get_tier_files()
    current_app = load_app_config()
    stats["app_config"] = {
        "path": str(APP_CONFIG_PATH.relative_to(APP_CONFIG_PATH.parent.parent)),
        "namespace": current_app.get("knowledge_namespace", "default"),
        "app_name": current_app.get("app_name", "企业知识库 Agent"),
    }
    ensure_system_prompt_file()
    stats["system_prompt"] = {
        "path": "data/prompts/system_prompt.md",
        "size": os.path.getsize(SYSTEM_PROMPT_PATH) if os.path.exists(SYSTEM_PROMPT_PATH) else 0,
        "mtime": os.path.getmtime(SYSTEM_PROMPT_PATH) if os.path.exists(SYSTEM_PROMPT_PATH) else 0,
    }
    return stats


@app.get("/api/admin/app-config")
async def admin_get_app_config(request: Request):
    verify_admin(request)
    ensure_app_config_file()
    return {
        "ok": True,
        "content": json.dumps(load_app_config(), ensure_ascii=False, indent=2),
        "path": str(APP_CONFIG_PATH.relative_to(APP_CONFIG_PATH.parent.parent)),
        "mtime": os.path.getmtime(APP_CONFIG_PATH),
        "size": os.path.getsize(APP_CONFIG_PATH),
    }


@app.put("/api/admin/app-config")
async def admin_update_app_config(request: Request):
    verify_admin(request)
    body = await request.json()
    content = body.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return JSONResponse({"ok": False, "msg": "配置不能为空"}, status_code=400)
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        return JSONResponse({"ok": False, "msg": f"JSON 格式错误: {exc}"}, status_code=400)
    save_app_config(parsed)
    count = rag_engine.build_index()
    semantic_cache.clear()
    return {
        "ok": True,
        "msg": f"业务配置已保存，当前知识片段 {count} 个",
        "path": str(APP_CONFIG_PATH.relative_to(APP_CONFIG_PATH.parent.parent)),
        "mtime": os.path.getmtime(APP_CONFIG_PATH),
        "size": os.path.getsize(APP_CONFIG_PATH),
    }


@app.get("/api/admin/release-profiles")
async def admin_release_profiles(request: Request):
    """平台管理员：查看可导出的销售版本。"""
    verify_admin(request)
    cfg = load_app_config()
    return {
        "ok": True,
        "current": {
            "edition": cfg.get("edition", "service_provider"),
            "deployment_mode": cfg.get("deployment_mode", "double_backend"),
            "feature_flags": cfg.get("feature_flags", {}),
            "release_profile": cfg.get("release_profile", {}),
        },
        "profiles": list_release_profiles(),
    }


@app.post("/api/admin/release/export")
async def admin_export_release(request: Request):
    """平台管理员：导出销售版本打包文件。"""
    verify_admin(request)
    body = await request.json()
    profile_key = str(body.get("profile_key", "") or "").strip()
    if not profile_key:
        return JSONResponse({"ok": False, "msg": "缺少 profile_key"}, status_code=400)
    try:
        result = export_release_bundle(profile_key=profile_key, current_app_config=load_app_config())
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {
        "ok": True,
        "msg": f"已导出 {result['profile']['label']} 打包文件",
        "profile": result["profile"],
        "zip_name": result["zip_name"],
        "zip_path": result["zip_path"],
        "download_url": f"/api/admin/release/download?zip_name={result['zip_name']}",
    }


@app.get("/api/admin/release/download")
async def admin_download_release(request: Request, zip_name: str = ""):
    """平台管理员：下载导出的版本包。"""
    verify_admin(request)
    safe_name = os.path.basename(zip_name)
    if not safe_name.endswith(".zip"):
        return JSONResponse({"ok": False, "msg": "无效的压缩包名称"}, status_code=400)
    zip_path = os.path.join(os.path.dirname(__file__), "..", "output", "releases", safe_name)
    if not os.path.isfile(zip_path):
        return JSONResponse({"ok": False, "msg": "导出文件不存在"}, status_code=404)
    return FileResponse(zip_path, filename=safe_name, media_type="application/zip")


@app.get("/api/admin/model-config")
async def admin_get_model_config(request: Request):
    verify_admin(request)
    config_data = load_model_config()
    return {
        "ok": True,
        "config": {
            "base_url": config_data.get("base_url", ""),
            "model_primary": config_data.get("model_primary", ""),
            "model_fallback": config_data.get("model_fallback", ""),
            "providers": config_data.get("providers", []),
        },
        "api_keys_text": "\n".join(config_data.get("api_keys", [])),
    }


@app.get("/api/admin/retrieval-config")
async def admin_get_retrieval_config(request: Request):
    verify_admin(request)
    ensure_retrieval_config_file()
    return {
        "ok": True,
        "config": load_retrieval_config(),
        "path": str(RETRIEVAL_CONFIG_PATH.relative_to(RETRIEVAL_CONFIG_PATH.parent.parent)),
        "mtime": os.path.getmtime(RETRIEVAL_CONFIG_PATH),
        "size": os.path.getsize(RETRIEVAL_CONFIG_PATH),
    }


@app.put("/api/admin/retrieval-config")
async def admin_update_retrieval_config(request: Request):
    verify_admin(request)
    body = await request.json()
    config_data = body.get("config")
    if not isinstance(config_data, dict):
        return JSONResponse({"ok": False, "msg": "缺少检索配置对象"}, status_code=400)
    try:
        saved = save_retrieval_config(config_data)
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    count = rag_engine.build_index()
    semantic_cache.clear()
    return {
        "ok": True,
        "msg": f"检索配置已保存，当前知识片段 {count} 个",
        "config": saved,
    }


@app.put("/api/admin/model-config")
async def admin_update_model_config(request: Request):
    verify_admin(request)
    body = await request.json()
    try:
        config_data = save_model_config(
            {
                "base_url": body.get("base_url", ""),
                "model_primary": body.get("model_primary", ""),
                "model_fallback": body.get("model_fallback", ""),
                "providers": body.get("providers", []),
            },
            body.get("api_keys_text", ""),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    semantic_cache.clear()
    return {
        "ok": True,
        "msg": f"模型配置已保存，当前 Key 数量 {len(config_data.get('api_keys', []))} 个",
        "config": config_data,
    }

@app.get("/api/admin/crawler-config")
async def admin_get_crawler_config(request: Request):
    verify_admin(request)
    ensure_crawler_config_file()
    return {
        "ok": True,
        "items": _crawler_sources_with_status(),
        "path": "data/crawler_config.json",
        "mtime": os.path.getmtime(PLATFORM_CRAWLER_CONFIG_PATH) if os.path.exists(PLATFORM_CRAWLER_CONFIG_PATH) else 0,
        "size": os.path.getsize(PLATFORM_CRAWLER_CONFIG_PATH) if os.path.exists(PLATFORM_CRAWLER_CONFIG_PATH) else 0,
    }


@app.put("/api/admin/crawler-config")
async def admin_update_crawler_config(request: Request):
    verify_admin(request)
    body = await request.json()
    items = body.get("items", [])
    try:
        saved = save_crawler_sources(items)
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {
        "ok": True,
        "msg": f"采集源配置已保存，共 {len(saved)} 条",
        "items": _crawler_sources_with_status(),
    }


@app.post("/api/admin/crawler-config/run")
async def admin_run_crawler_config(request: Request):
    verify_admin(request)
    body = await request.json()
    source_id = str(body.get("source_id", "")).strip()
    if not source_id:
        return JSONResponse({"ok": False, "msg": "缺少 source_id"}, status_code=400)
    items = load_crawler_sources()
    target = next((item for item in items if item.get("source_id") == source_id), None)
    if not target:
        return JSONResponse({"ok": False, "msg": "未找到对应采集源"}, status_code=404)
    try:
        result = run_generic_crawler(target, _knowledge_dir())
        if int(result.items_count or 0) <= 0:
            payload = {
                "ok": False,
                "msg": "正文抽取失败：页面已访问成功，但没有识别出可入库正文。",
                "output_file": result.output_file,
                "items": _crawler_sources_with_status(),
            }
            run_status = "failed"
            detail = "正文抽取失败：页面已访问成功，但没有识别出可入库正文。"
        else:
            count = rag_engine.build_index()
            semantic_cache.clear()
            payload = {
                "ok": True,
                "msg": result.message,
                "chunks": count,
                "output_file": result.output_file,
                "items": _crawler_sources_with_status(),
            }
            run_status = "success"
            detail = f"{result.title} · {result.items_count} 行"
    except Exception as exc:
        payload = {
            "ok": False,
            "msg": str(exc),
            "items": _crawler_sources_with_status(),
        }
        run_status = "failed"
        detail = str(exc)
    record_crawler_run(
        source_id=source_id,
        source_name=target.get("name", source_id) if target else source_id,
        status=run_status,
        tier=target.get("tier", "") if target else "",
        items_count=int(result.items_count if run_status == "success" else 0),
        detail=detail,
    )
    payload["items"] = _crawler_sources_with_status()
    return payload


@app.get("/api/admin/crawler-runs")
async def admin_crawler_runs(request: Request, page: int = 1):
    """平台总后台：查看采集执行历史。"""
    verify_admin(request)
    logs, total = list_crawler_runs(page=page, per_page=20)
    return {"ok": True, "logs": logs, "total": total, "page": page}


@app.get("/api/admin/crawler-scheduler")
async def admin_crawler_scheduler_status(request: Request):
    """平台总后台：查看调度器状态。"""
    verify_admin(request)
    return {"ok": True, "status": crawler_scheduler.health()}

@app.post("/api/admin/knowledge/rebuild")
async def admin_rebuild_knowledge(request: Request):
    verify_admin(request)
    count = rag_engine.build_index()
    semantic_cache.clear()
    return {"ok": True, "msg": f"向量索引重建完成，共 {count} 个知识片段"}

@app.get("/api/admin/knowledge/file")
async def admin_get_file(request: Request, tier: str = "", file: str = ""):
    """Get file content for editing."""
    verify_admin(request)
    if not file:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    fpath = _find_knowledge_file_path(_knowledge_dir(), file)
    if not os.path.isfile(fpath) or not file.endswith(".md"):
        return JSONResponse({"ok": False, "msg": "文件不存在"}, status_code=404)
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    rel_path = Path(fpath).relative_to(_knowledge_dir()).as_posix()
    return {"ok": True, "content": content, "file": file, "tier": "knowledge", "file_key": rel_path}


@app.get("/api/admin/system-prompt")
async def admin_get_system_prompt(request: Request):
    verify_admin(request)
    ensure_system_prompt_file()
    with open(SYSTEM_PROMPT_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    return {
        "ok": True,
        "content": content,
        "path": "data/prompts/system_prompt.md",
        "mtime": os.path.getmtime(SYSTEM_PROMPT_PATH),
        "size": os.path.getsize(SYSTEM_PROMPT_PATH),
    }


@app.get("/api/tenant/app-config")
async def tenant_get_app_config(request: Request):
    """租户后台：读取本租户企业配置。"""
    tenant = verify_tenant_admin_request(request)
    config = load_tenant_app_config(tenant["tenant_id"], tenant["tenant_name"])
    paths = get_tenant_paths(tenant["tenant_id"])
    config_path = paths["config"]
    return {
        "ok": True,
        "tenant": tenant,
        "config": config,
        "path": config_path,
        "mtime": os.path.getmtime(config_path),
        "size": os.path.getsize(config_path),
    }


@app.put("/api/tenant/app-config")
async def tenant_update_app_config(request: Request):
    """租户后台：保存本租户企业配置。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    config = body.get("config")
    if not isinstance(config, dict):
        return JSONResponse({"ok": False, "msg": "缺少租户配置对象"}, status_code=400)
    saved = save_tenant_app_config(tenant["tenant_id"], tenant["tenant_name"], config)
    return {"ok": True, "msg": "租户配置已保存", "config": saved}


@app.get("/api/tenant/system-prompt")
async def tenant_get_system_prompt(request: Request):
    """租户后台：读取本租户提示词。"""
    tenant = verify_tenant_admin_request(request)
    paths = get_tenant_paths(tenant["tenant_id"])
    content = load_tenant_system_prompt(tenant["tenant_id"], tenant["tenant_name"])
    return {
        "ok": True,
        "tenant": tenant,
        "content": content,
        "path": paths["prompt"],
        "mtime": os.path.getmtime(paths["prompt"]),
        "size": os.path.getsize(paths["prompt"]),
    }


@app.put("/api/tenant/system-prompt")
async def tenant_update_system_prompt(request: Request):
    """租户后台：保存本租户提示词。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    content = str(body.get("content", "")).strip()
    if not content:
        return JSONResponse({"ok": False, "msg": "提示词不能为空"}, status_code=400)
    try:
        save_tenant_system_prompt(tenant["tenant_id"], tenant["tenant_name"], content)
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {"ok": True, "msg": "租户提示词已保存"}


@app.get("/api/tenant/knowledge/overview")
async def tenant_knowledge_overview(request: Request):
    """租户后台：查看本租户知识空间位置。"""
    tenant = verify_tenant_admin_request(request)
    paths = get_tenant_paths(tenant["tenant_id"])
    knowledge_root = os.path.join(os.path.dirname(__file__), "..", "knowledge", tenant["tenant_id"])
    return {
        "ok": True,
        "tenant": tenant,
        "knowledge_namespace": tenant["tenant_id"],
        "knowledge_root": os.path.abspath(knowledge_root),
        "config_path": paths["config"],
        "prompt_path": paths["prompt"],
        "model_config_path": paths["model_config"],
        "retrieval_config_path": paths["retrieval_config"],
        "crawler_config_path": paths["crawler_config"],
    }


@app.get("/api/tenant/model-config")
async def tenant_get_model_config(request: Request):
    """租户后台：读取本租户模型配置。"""
    tenant = verify_tenant_admin_request(request)
    config_data = load_model_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    paths = get_tenant_paths(tenant["tenant_id"])
    model_path = paths["model_config"]
    keys_path = paths["api_keys"]
    return {
        "ok": True,
        "tenant": tenant,
        "config": {
            "base_url": config_data.get("base_url", ""),
            "model_primary": config_data.get("model_primary", ""),
            "model_fallback": config_data.get("model_fallback", ""),
            "providers": config_data.get("providers", []),
        },
        "api_keys_text": "\n".join(config_data.get("api_keys", [])),
        "path": model_path,
        "keys_path": keys_path,
        "mtime": os.path.getmtime(model_path),
        "size": os.path.getsize(model_path),
    }


@app.put("/api/tenant/model-config")
async def tenant_update_model_config(request: Request):
    """租户后台：保存本租户模型配置。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    try:
        saved = save_model_config(
            {
                "base_url": body.get("base_url", ""),
                "model_primary": body.get("model_primary", ""),
                "model_fallback": body.get("model_fallback", ""),
                "providers": body.get("providers", []),
            },
            body.get("api_keys_text", ""),
            tenant_id=tenant["tenant_id"],
            tenant_name=tenant["tenant_name"],
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {"ok": True, "msg": "租户模型配置已保存", "config": saved}


@app.get("/api/tenant/retrieval-config")
async def tenant_get_retrieval_config(request: Request):
    """租户后台：读取本租户检索配置。"""
    tenant = verify_tenant_admin_request(request)
    config_data = load_retrieval_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    paths = get_tenant_paths(tenant["tenant_id"])
    config_path = paths["retrieval_config"]
    return {
        "ok": True,
        "tenant": tenant,
        "config": config_data,
        "path": config_path,
        "mtime": os.path.getmtime(config_path),
        "size": os.path.getsize(config_path),
    }


@app.put("/api/tenant/retrieval-config")
async def tenant_update_retrieval_config(request: Request):
    """租户后台：保存本租户检索配置。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    config_data = body.get("config")
    if not isinstance(config_data, dict):
        return JSONResponse({"ok": False, "msg": "缺少检索配置对象"}, status_code=400)
    try:
        saved = save_retrieval_config(
            config_data,
            tenant_id=tenant["tenant_id"],
            tenant_name=tenant["tenant_name"],
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {"ok": True, "msg": "租户检索配置已保存", "config": saved}


@app.get("/api/tenant/crawler-config")
async def tenant_get_crawler_config(request: Request):
    """租户后台：读取本租户脚本配置。"""
    tenant = verify_tenant_admin_request(request)
    paths = get_tenant_paths(tenant["tenant_id"])
    config_path = paths["crawler_config"]
    ensure_crawler_config_file(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    return {
        "ok": True,
        "tenant": tenant,
        "items": _crawler_sources_with_status(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"]),
        "path": config_path,
        "mtime": os.path.getmtime(config_path),
        "size": os.path.getsize(config_path),
    }


@app.put("/api/tenant/crawler-config")
async def tenant_update_crawler_config(request: Request):
    """租户后台：保存本租户脚本配置。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    items = body.get("items", [])
    try:
        save_crawler_sources(items, tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {
        "ok": True,
        "msg": "租户脚本配置已保存",
        "items": _crawler_sources_with_status(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"]),
    }


@app.get("/api/tenant/tool-config")
async def tenant_get_tool_config(request: Request):
    """租户后台：读取本租户工具配置。"""
    tenant = verify_tenant_admin_request(request)
    config_data = load_tool_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    paths = get_tenant_paths(tenant["tenant_id"])
    config_path = paths["tool_config"]
    return {
        "ok": True,
        "tenant": tenant,
        "config": config_data,
        "path": config_path,
        "mtime": os.path.getmtime(config_path),
        "size": os.path.getsize(config_path),
    }


@app.put("/api/tenant/tool-config")
async def tenant_update_tool_config(request: Request):
    """租户后台：保存本租户工具配置。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    config_data = body.get("config")
    if not isinstance(config_data, dict):
        return JSONResponse({"ok": False, "msg": "缺少工具配置对象"}, status_code=400)
    try:
        saved = save_tool_config(
            config_data,
            tenant_id=tenant["tenant_id"],
            tenant_name=tenant["tenant_name"],
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {"ok": True, "msg": "租户工具配置已保存", "config": saved}

@app.get("/api/tenant/workflows")
async def tenant_get_workflows(request: Request):
    """租户后台：读取本租户工作流配置。"""
    tenant = verify_tenant_admin_request(request)
    workflow_cfg = load_workflow_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    model_cfg = load_model_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    retrieval_cfg = load_retrieval_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    tool_cfg = load_tool_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    mcp_servers = list_enabled_mcp_servers(tool_cfg)
    paths = get_tenant_paths(tenant["tenant_id"])
    providers = model_cfg.get("providers", [])
    model_options: list[dict] = []
    for provider in providers:
        provider_label = str(provider.get("label") or provider.get("id") or "供应商")
        capability_label = "图文" if bool(provider.get("supports_image")) else "文本"
        for model_name in [provider.get("model_primary"), provider.get("model_fallback")]:
            text = str(model_name or "").strip()
            if text and not any(item["value"] == text for item in model_options):
                model_options.append(
                    {
                        "value": text,
                        "label": f"{provider_label} / {text} / {capability_label}",
                        "supports_image": bool(provider.get("supports_image")),
                        "capability_label": capability_label,
                    }
                )
    if not model_options:
        model_options.append({"value": "__default__", "label": "当前可用模型"})
    workflow_summaries = []
    for item in workflow_cfg.get("items", []):
        workflow_summaries.append(
            {
                "id": item.get("workflow_id"),
                "workflow_id": item.get("workflow_id"),
                "name": item.get("name"),
                "description": item.get("description", ""),
                "version": item.get("version", "V1.0"),
                "status": item.get("status", "draft"),
                "enabled": bool(item.get("enabled", True)),
                "updatedAt": item.get("updated_at", ""),
                "nodeCount": len(item.get("nodes") or []),
                "nodes": item.get("nodes") or [],
                "connections": item.get("connections") or [],
            }
        )
    return {
        "ok": True,
        "tenant": tenant,
        "default_workflow_id": workflow_cfg.get("default_workflow_id", ""),
        "items": workflow_summaries,
        "capabilities": {
            "models": model_options,
            "knowledge_bases": [
                {"value": "全部知识库", "label": "全部知识库"},
                *[
                    {"value": item.get("library_id", ""), "label": item.get("name", item.get("library_id", ""))}
                    for item in list_knowledge_libraries(tenant["tenant_id"], tenant["tenant_name"])
                ],
            ],
            "retrieval_backend": retrieval_cfg.get("backend", "hybrid"),
            "notification_channels": [
                {"value": "邮件", "label": "邮件"},
                {"value": "短信", "label": "短信"},
                {"value": "企业微信", "label": "企业微信"},
                {"value": "钉钉", "label": "钉钉"},
                {"value": "站内推送", "label": "站内推送"},
            ],
            "email_enabled": bool((tool_cfg.get("email") or {}).get("enabled")),
            "mcp_servers": [
                {
                    "value": item.get("server_id", ""),
                    "label": item.get("label", item.get("server_id", "")),
                }
                for item in mcp_servers
            ],
        },
        "path": paths["workflow_config"],
        "mtime": os.path.getmtime(paths["workflow_config"]),
        "size": os.path.getsize(paths["workflow_config"]),
    }


@app.put("/api/tenant/workflows")
async def tenant_update_workflows(request: Request):
    """租户后台：保存本租户工作流配置。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    items = body.get("items")
    if not isinstance(items, list):
        return JSONResponse({"ok": False, "msg": "缺少工作流数组"}, status_code=400)
    current = load_workflow_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    default_workflow_id = str(body.get("default_workflow_id") or current.get("default_workflow_id") or "").strip()
    try:
        saved = save_workflow_config(
            {
                "default_workflow_id": default_workflow_id,
                "items": items,
            },
            tenant_id=tenant["tenant_id"],
            tenant_name=tenant["tenant_name"],
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {"ok": True, "msg": "租户工作流已保存", **saved}


@app.post("/api/tenant/workflows/publish")
async def tenant_publish_workflow(request: Request):
    """租户后台：发布指定工作流。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    workflow_id = str(body.get("workflow_id") or "").strip()
    if not workflow_id:
        return JSONResponse({"ok": False, "msg": "缺少 workflow_id"}, status_code=400)
    config = load_workflow_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    items = list(config.get("items") or [])
    target = next((item for item in items if item.get("workflow_id") == workflow_id), None)
    if not target:
        return JSONResponse({"ok": False, "msg": "未找到对应工作流"}, status_code=404)
    target["status"] = "published"
    target["updated_at"] = _now_text()
    version = str(target.get("version") or "V1.0").lstrip("V")
    major, _, minor = version.partition(".")
    try:
        major_int = int(major or "1")
    except Exception:
        major_int = 1
    try:
        minor_int = int(minor or "0")
    except Exception:
        minor_int = 0
    target["version"] = f"V{major_int}.{minor_int + 1}"
    if not str(config.get("default_workflow_id") or "").strip():
        config["default_workflow_id"] = workflow_id
    saved = save_workflow_config(config, tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    return {"ok": True, "msg": "工作流已发布", **saved}


@app.post("/api/tenant/workflows/unpublish")
async def tenant_unpublish_workflow(request: Request):
    """租户后台：取消发布指定工作流。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    workflow_id = str(body.get("workflow_id") or "").strip()
    if not workflow_id:
        return JSONResponse({"ok": False, "msg": "缺少 workflow_id"}, status_code=400)
    config = load_workflow_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    items = list(config.get("items") or [])
    target = next((item for item in items if item.get("workflow_id") == workflow_id), None)
    if not target:
        return JSONResponse({"ok": False, "msg": "未找到对应工作流"}, status_code=404)
    target["status"] = "draft"
    target["updated_at"] = _now_text()
    if str(config.get("default_workflow_id") or "").strip() == workflow_id:
        config["default_workflow_id"] = ""
    saved = save_workflow_config(config, tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    return {"ok": True, "msg": "工作流已取消发布", **saved}


@app.delete("/api/tenant/workflows/{workflow_id}")
async def tenant_delete_workflow(request: Request, workflow_id: str):
    """租户后台：删除指定工作流。"""
    tenant = verify_tenant_admin_request(request)
    workflow_id = str(workflow_id or "").strip()
    if not workflow_id:
        return JSONResponse({"ok": False, "msg": "缺少 workflow_id"}, status_code=400)
    config = load_workflow_config(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    items = list(config.get("items") or [])
    remained = [item for item in items if str(item.get("workflow_id") or "").strip() != workflow_id]
    if len(remained) == len(items):
        return JSONResponse({"ok": False, "msg": "未找到对应工作流"}, status_code=404)
    if str(config.get("default_workflow_id") or "").strip() == workflow_id:
        config["default_workflow_id"] = str((remained[0].get("workflow_id") if remained else "") or "").strip()
    config["items"] = remained
    saved = save_workflow_config(config, tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    return {"ok": True, "msg": "工作流已删除", **saved}


@app.post("/api/tenant/workflows/run")
async def tenant_run_workflow(request: Request):
    """租户后台：试运行指定工作流。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    workflow_id = str(body.get("workflow_id") or "").strip()
    input_payload = body.get("input")
    if input_payload is None:
        input_payload = {}
    if not isinstance(input_payload, dict):
        return JSONResponse({"ok": False, "msg": "输入参数必须是 JSON 对象"}, status_code=400)
    try:
        result = await execute_tenant_workflow(
            tenant_id=tenant["tenant_id"],
            tenant_name=tenant["tenant_name"],
            workflow_id=workflow_id or None,
            input_payload=input_payload,
        )
    except WorkflowRuntimeError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "msg": f"工作流运行失败: {exc}"}, status_code=500)
    return {"ok": True, "result": result}


@app.post("/api/tenant/crawler-config/run")
async def tenant_run_crawler_config(request: Request):
    """租户后台：执行本租户的单条采集源。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    source_id = str(body.get("source_id", "")).strip()
    if not source_id:
        return JSONResponse({"ok": False, "msg": "缺少 source_id"}, status_code=400)
    items = load_crawler_sources(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    target = next((item for item in items if item.get("source_id") == source_id), None)
    if not target:
        return JSONResponse({"ok": False, "msg": "未找到对应采集源"}, status_code=404)
    result = None
    try:
        result = run_generic_crawler(target, get_tenant_knowledge_dir(tenant["tenant_id"]))
        if int(result.items_count or 0) <= 0:
            run_status = "failed"
            detail = "正文抽取失败：页面已访问成功，但没有识别出可入库正文。"
            payload = {
                "ok": False,
                "msg": detail,
                "output_file": result.output_file,
            }
        else:
            run_status = "success"
            detail = f"{result.title} · {result.items_count} 行"
            payload = {
                "ok": True,
                "msg": result.message,
                "output_file": result.output_file,
            }
    except Exception as exc:
        run_status = "failed"
        detail = str(exc)
        payload = {"ok": False, "msg": str(exc)}
    record_crawler_run(
        source_id=source_id,
        source_name=target.get("name", source_id),
        status=run_status,
        tier=target.get("tier", ""),
        items_count=int(result.items_count if result and run_status == "success" else 0),
        detail=detail,
        tenant_id=tenant["tenant_id"],
    )
    payload["items"] = _crawler_sources_with_status(tenant_id=tenant["tenant_id"], tenant_name=tenant["tenant_name"])
    return payload


@app.get("/api/tenant/crawler-runs")
async def tenant_crawler_runs(request: Request, page: int = 1):
    """租户后台：查看本租户脚本执行历史。"""
    tenant = verify_tenant_admin_request(request)
    logs, total = list_crawler_runs(page=page, per_page=20, tenant_id=tenant["tenant_id"])
    return {"ok": True, "logs": logs, "total": total, "page": page}


@app.post("/api/tenant/crawler-runs/clear")
async def tenant_clear_crawler_runs(request: Request):
    """租户后台：清空本租户脚本执行历史。"""
    tenant = verify_tenant_admin_request(request)
    deleted = clear_crawler_runs(tenant_id=tenant["tenant_id"])
    return {"ok": True, "msg": f"已清空 {deleted} 条执行历史", "deleted": deleted}


@app.get("/api/tenant/crawler-scheduler")
async def tenant_crawler_scheduler_status(request: Request):
    """租户后台：查看平台调度器状态。"""
    tenant = verify_tenant_admin_request(request)
    return {"ok": True, "status": crawler_scheduler.health(tenant_id=tenant["tenant_id"])}


@app.get("/api/tenant/knowledge/files")
async def tenant_knowledge_files(request: Request):
    """租户后台：列出本租户知识文件。"""
    tenant = verify_tenant_admin_request(request)
    tenant_id = str(tenant.get("tenant_id") or "default").strip() or "default"
    knowledge_root = get_tenant_knowledge_dir(tenant_id)
    os.makedirs(knowledge_root, exist_ok=True)
    app_settings = load_tenant_app_config(tenant_id, tenant.get("tenant_name", ""))
    knowledge_namespace = str(app_settings.get("knowledge_namespace") or "").strip().lower()
    candidate_roots = [knowledge_root]
    if knowledge_namespace and knowledge_namespace != tenant_id:
        namespace_root = str((Path(__file__).resolve().parent.parent / "knowledge" / knowledge_namespace))
        if namespace_root not in candidate_roots:
            candidate_roots.append(namespace_root)
    selected_root = knowledge_root
    selected_files: list[str] = []
    for root in candidate_roots:
        os.makedirs(root, exist_ok=True)
        files = _iter_knowledge_markdown_files(root)
        if files:
            selected_root = root
            selected_files = files
            break
    if not selected_files:
        selected_files = _iter_knowledge_markdown_files(selected_root)
    available_tags = list_knowledge_tags(tenant["tenant_id"], tenant.get("tenant_name", ""))
    tag_groups = list_knowledge_tag_groups(tenant["tenant_id"], tenant.get("tenant_name", ""))
    libraries = list_knowledge_libraries(tenant["tenant_id"], tenant.get("tenant_name", ""))
    categories = list_knowledge_categories(tenant["tenant_id"], tenant.get("tenant_name", ""))
    categories_by_library: dict[str, list[dict]] = defaultdict(list)
    for item in categories:
        categories_by_library[str(item.get("library_id") or "")].append(dict(item))
    library_index = {str(item.get("library_id") or ""): dict(item) for item in libraries}
    all_files: list[dict] = []
    library_files: dict[str, list[dict]] = defaultdict(list)
    category_counts: dict[str, int] = defaultdict(int)
    for fpath in selected_files:
        name = Path(fpath).name
        rel_path = Path(fpath).relative_to(selected_root).as_posix()
        inferred_tier = rel_path.split("/", 1)[0] if "/" in rel_path else "permanent"
        if inferred_tier not in _knowledge_tiers():
            inferred_tier = "permanent"
        preview = ""
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                preview = f.read(240).replace("\n", " ").strip()
        except Exception:
            preview = ""
        file_meta = {
            "name": name,
            "file_key": rel_path,
            "size": os.path.getsize(fpath),
            "mtime": os.path.getmtime(fpath),
            "preview": preview,
            **get_knowledge_file_meta(
                tenant["tenant_id"],
                inferred_tier,
                rel_path,
                tenant.get("tenant_name", ""),
            ),
        }
        all_files.append(file_meta)
        library_id = str(file_meta.get("library_id") or libraries[0]["library_id"])
        category_id = str(file_meta.get("category_id") or "")
        library_files[library_id].append(file_meta)
        if category_id:
            category_counts[category_id] += 1
    library_payload: list[dict] = []
    for library in libraries:
        library_id = str(library.get("library_id") or "")
        files = library_files.get(library_id, [])
        library_payload.append(
            {
                **library,
                "file_count": len(files),
                "categories": [
                    {
                        **item,
                        "file_count": int(category_counts.get(str(item.get("category_id") or ""), 0)),
                    }
                    for item in categories_by_library.get(library_id, [])
                ],
            }
        )
    return {
        "ok": True,
        "tenant": tenant,
        "items": [
            {
                "tier": "knowledge",
                "tier_label": "知识文件",
                "files": all_files,
            }
        ],
        "libraries": library_payload,
        "categories": categories,
        "available_tags": available_tags,
        "tag_catalog": available_tags,
        "tag_groups": tag_groups,
    }


@app.put("/api/tenant/knowledge/libraries")
async def tenant_save_knowledge_libraries(request: Request):
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    libraries = body.get("libraries") if isinstance(body.get("libraries"), list) else []
    categories = body.get("categories") if isinstance(body.get("categories"), list) else []
    metadata = save_knowledge_structure(
        tenant["tenant_id"],
        libraries=libraries,
        categories=categories,
        tenant_name=tenant.get("tenant_name", ""),
    )
    return {
        "ok": True,
        "libraries": metadata.get("libraries") or [],
        "categories": metadata.get("categories") or [],
        "msg": "知识库结构已保存",
    }


@app.get("/api/tenant/knowledge/download")
async def tenant_download_knowledge_file(request: Request, tier: str = "", file: str = ""):
    """租户后台：下载本租户知识文件。"""
    tenant = verify_tenant_admin_request(request)
    if not file:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    fpath = _find_knowledge_file_path(get_tenant_knowledge_dir(tenant["tenant_id"]), file)
    if not os.path.isfile(fpath):
        return JSONResponse({"ok": False, "msg": "文件不存在"}, status_code=404)
    return FileResponse(fpath, filename=file, media_type="text/markdown")


@app.get("/api/tenant/knowledge/file")
async def tenant_get_knowledge_file_content(request: Request, tier: str = "", file: str = ""):
    """租户后台：读取本租户知识文件内容，供在线编辑。"""
    tenant = verify_tenant_admin_request(request)
    if not file:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    fpath = _find_knowledge_file_path(get_tenant_knowledge_dir(tenant["tenant_id"]), file)
    if not os.path.isfile(fpath):
        return JSONResponse({"ok": False, "msg": "文件不存在"}, status_code=404)
    try:
        content = Path(fpath).read_text(encoding="utf-8")
    except Exception as exc:
        return JSONResponse({"ok": False, "msg": f"读取失败：{exc}"}, status_code=500)
    rel_path = Path(fpath).relative_to(get_tenant_knowledge_dir(tenant["tenant_id"])).as_posix()
    inferred_tier = rel_path.split("/", 1)[0] if "/" in rel_path else "permanent"
    if inferred_tier not in _knowledge_tiers():
        inferred_tier = "permanent"
    meta = get_knowledge_file_meta(tenant["tenant_id"], inferred_tier, rel_path, tenant.get("tenant_name", ""))
    return {
        "ok": True,
        "tier": "knowledge",
        "file": file,
        "file_key": rel_path,
        "content": content,
        "tags": meta.get("tags", []),
        "library_id": meta.get("library_id", ""),
        "category_id": meta.get("category_id", ""),
        "source_type": meta.get("source_type", ""),
        "display_mode": meta.get("display_mode", ""),
        "preview": meta.get("preview", {}),
    }


@app.get("/api/tenant/knowledge/tags")
async def tenant_knowledge_tags(request: Request):
    tenant = verify_tenant_admin_request(request)
    library_id = str(request.query_params.get("library_id", "") or "").strip()
    tags = list_knowledge_tags(tenant["tenant_id"], tenant.get("tenant_name", ""), library_id=library_id)
    groups = list_knowledge_tag_groups(tenant["tenant_id"], tenant.get("tenant_name", ""), library_id=library_id)
    return {"ok": True, "items": tags, "groups": groups}


@app.put("/api/tenant/knowledge/tags")
async def tenant_save_knowledge_tags(request: Request):
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    library_id = str(body.get("library_id") or request.query_params.get("library_id", "") or "").strip()
    raw_groups = body.get("groups") if isinstance(body.get("groups"), list) else None
    if raw_groups is not None:
        groups = save_knowledge_tag_groups(tenant["tenant_id"], raw_groups, tenant.get("tenant_name", ""), library_id=library_id)
        tags = list_knowledge_tags(tenant["tenant_id"], tenant.get("tenant_name", ""), library_id=library_id)
        return {"ok": True, "items": tags, "groups": groups, "msg": "标签目录已保存"}
    raw_tags = body.get("tags") if isinstance(body.get("tags"), list) else []
    tags = save_knowledge_tag_catalog(tenant["tenant_id"], raw_tags, tenant.get("tenant_name", ""), library_id=library_id)
    groups = list_knowledge_tag_groups(tenant["tenant_id"], tenant.get("tenant_name", ""), library_id=library_id)
    return {"ok": True, "items": tags, "groups": groups, "msg": "标签目录已保存"}


@app.get("/api/tenant/knowledge/file/chunks")
async def tenant_knowledge_file_chunks(request: Request):
    tenant = verify_tenant_admin_request(request)
    file = str(request.query_params.get("file", "")).strip()
    if not file:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    fpath = _find_knowledge_file_path(get_tenant_knowledge_dir(tenant["tenant_id"]), file)
    if not os.path.isfile(fpath):
        return JSONResponse({"ok": False, "msg": "文件不存在"}, status_code=404)
    try:
        content = Path(fpath).read_text(encoding="utf-8")
    except Exception:
        content = Path(fpath).read_text(encoding="utf-8", errors="ignore")
    rel_path = Path(fpath).relative_to(get_tenant_knowledge_dir(tenant["tenant_id"])).as_posix()
    inferred_tier = rel_path.split("/", 1)[0] if "/" in rel_path else "permanent"
    if inferred_tier not in _knowledge_tiers():
        inferred_tier = "permanent"
    meta = get_knowledge_file_meta(tenant["tenant_id"], inferred_tier, rel_path, tenant.get("tenant_name", ""))
    try:
        chunks = split_documents_for_stats(content)
    except Exception:
        # 解析详情是观测能力，不能因为切片异常影响页面使用。
        fallback_blocks = [block.strip() for block in content.replace("\r", "").split("\n\n") if block.strip()]
        if not fallback_blocks:
            fallback_blocks = [content.strip()] if content.strip() else []
        chunks = []
        for block in fallback_blocks:
            chunks.append(type("SimpleChunk", (), {"page_content": block, "metadata": {}})())
    return {
        "ok": True,
        "tier": "knowledge",
        "file": file,
        "size": os.path.getsize(fpath),
        "chunk_count": len(chunks),
        "content": content,
        "source_type": meta.get("source_type", ""),
        "original_suffix": meta.get("original_suffix", ""),
        "display_mode": meta.get("display_mode", ""),
        "parser_chain": meta.get("parser_chain", []),
        "preview": meta.get("preview", {}),
        "chunks": [
            {
                "index": index + 1,
                "text": str(doc.page_content or "").strip(),
                "meta": dict(doc.metadata or {}),
            }
            for index, doc in enumerate(chunks)
        ],
    }


@app.put("/api/tenant/knowledge/file")
async def tenant_update_knowledge_file(request: Request):
    """租户后台：保存本租户知识文件内容。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    file = str(body.get("file", "")).strip()
    content = str(body.get("content", ""))
    raw_tags = body.get("tags") if isinstance(body.get("tags"), list) else None
    library_id = str(body.get("library_id") or "").strip()
    category_id = str(body.get("category_id") or "").strip()
    if not file:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    fpath = _find_knowledge_file_path(get_tenant_knowledge_dir(tenant["tenant_id"]), file)
    if not os.path.isfile(fpath):
        return JSONResponse({"ok": False, "msg": "文件不存在"}, status_code=404)
    try:
        Path(fpath).write_text(content, encoding="utf-8")
    except Exception as exc:
        return JSONResponse({"ok": False, "msg": f"保存失败：{exc}"}, status_code=500)
    rel_path = Path(fpath).relative_to(get_tenant_knowledge_dir(tenant["tenant_id"])).as_posix()
    inferred_tier = rel_path.split("/", 1)[0] if "/" in rel_path else "permanent"
    if inferred_tier not in _knowledge_tiers():
        inferred_tier = "permanent"
    if raw_tags is not None or library_id or category_id:
        set_knowledge_file_meta(
            tenant["tenant_id"],
            tier=inferred_tier,
            file_name=rel_path,
            tags=raw_tags,
            library_id=library_id,
            category_id=category_id,
            tenant_name=tenant.get("tenant_name", ""),
        )
    meta = get_knowledge_file_meta(tenant["tenant_id"], inferred_tier, rel_path, tenant.get("tenant_name", ""))
    return {
        "ok": True,
        "msg": f"已保存 {file}",
        "tags": meta.get("tags", []),
        "library_id": meta.get("library_id", ""),
        "category_id": meta.get("category_id", ""),
    }


@app.post("/api/tenant/knowledge/upload")
async def tenant_upload_knowledge(request: Request, file: UploadFile = File(...)):
    """租户后台：上传本租户知识文件。"""
    tenant = verify_tenant_admin_request(request)
    upload_tags = [
        item.strip()
        for item in str(request.query_params.get("tags", "") or "").replace("，", ",").split(",")
        if item.strip()
    ]
    library_id = str(request.query_params.get("library_id", "") or "").strip()
    category_id = str(request.query_params.get("category_id", "") or "").strip()
    content = await file.read()
    try:
        parsed = parse_uploaded_knowledge_file(
            filename=file.filename or "uploaded.md",
            raw_bytes=content,
            tier="permanent",
            temp_dir=Path(get_tenant_knowledge_dir(tenant["tenant_id"])) / ".upload_tmp",
        )
    except UnsupportedDocumentError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"ok": False, "msg": f"文档解析失败：{exc}"}, status_code=500)
    tenant_root = get_tenant_knowledge_dir(tenant["tenant_id"])
    storage_dir = _knowledge_storage_dir(tenant_root, library_id, category_id)
    _write_parsed_knowledge_file(tier_dir=storage_dir, parsed=parsed)
    stored_rel_path = Path(storage_dir, parsed.filename).relative_to(tenant_root).as_posix()
    set_knowledge_file_meta(
        tenant["tenant_id"],
        tier="permanent",
        file_name=stored_rel_path,
        tags=upload_tags,
        library_id=library_id,
        category_id=category_id,
        asset_meta=parsed.ingest_metadata,
        tenant_name=tenant.get("tenant_name", ""),
    )
    return {
        "ok": True,
        "msg": f"已上传并解析为 Markdown，已按知识库/分类归档；共生成 {parsed.document_count} 个文档、{parsed.chunk_count} 个检索切片。",
        "tier": "knowledge",
        "stored_file": parsed.filename,
        "file_key": stored_rel_path,
        "source_type": parsed.source_type,
    }


@app.post("/api/tenant/knowledge/upload-web")
async def tenant_upload_web_knowledge(request: Request):
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    urls = body.get("urls") if isinstance(body.get("urls"), list) else []
    urls = [str(item or "").strip() for item in urls if str(item or "").strip()]
    if not urls:
        return JSONResponse({"ok": False, "msg": "请至少填写一个网页地址"}, status_code=400)
    library_id = str(body.get("library_id") or "").strip()
    category_id = str(body.get("category_id") or "").strip()
    tags = body.get("tags") if isinstance(body.get("tags"), list) else []
    knowledge_root = get_tenant_knowledge_dir(tenant["tenant_id"])
    library_lookup = {str(item.get("library_id") or ""): dict(item) for item in list_knowledge_libraries(tenant["tenant_id"], tenant.get("tenant_name", ""))}
    category_lookup = {str(item.get("category_id") or ""): dict(item) for item in list_knowledge_categories(tenant["tenant_id"], tenant.get("tenant_name", ""))}
    results: list[dict] = []
    for index, url in enumerate(urls, start=1):
        source = {
            "source_id": f"web_{uuid.uuid4().hex[:8]}",
            "name": f"网页导入 {index}",
            "url": url,
            "tier": "permanent",
            "source_type": "web",
            "rule_text": "",
            "library_id": library_id,
            "category_id": category_id,
            "library_name": str((library_lookup.get(library_id) or {}).get("name") or ""),
            "category_name": str((category_lookup.get(category_id) or {}).get("name") or ""),
        }
        try:
            result = run_generic_crawler(source, knowledge_root)
        except GenericCrawlerError as exc:
            results.append({"url": url, "ok": False, "msg": str(exc)})
            continue
        except Exception as exc:
            results.append({"url": url, "ok": False, "msg": f"网页导入失败：{exc}"})
            continue
        output_path = Path(result.output_file)
        output_name = output_path.name
        stored_rel_path = output_path.relative_to(knowledge_root).as_posix()
        set_knowledge_file_meta(
            tenant["tenant_id"],
            tier=result.tier,
            file_name=stored_rel_path,
            tags=tags,
            library_id=library_id,
            category_id=category_id,
            tenant_name=tenant.get("tenant_name", ""),
        )
        results.append(
            {
                "url": url,
                "ok": True,
                "stored_file": output_name,
                "file_key": stored_rel_path,
                "title": result.title,
                "items_count": result.items_count,
            }
        )
    success_count = len([item for item in results if item.get("ok")])
    return {"ok": success_count > 0, "items": results, "msg": f"已完成 {success_count}/{len(urls)} 个网页导入"}


@app.delete("/api/tenant/knowledge/file")
async def tenant_delete_knowledge_file(request: Request, tier: str = "", file: str = ""):
    """租户后台：删除本租户知识文件。"""
    tenant = verify_tenant_admin_request(request)
    if not file:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    knowledge_root = get_tenant_knowledge_dir(tenant["tenant_id"])
    fpath = _find_knowledge_file_path(knowledge_root, file)
    if not os.path.isfile(fpath):
        return JSONResponse({"ok": False, "msg": "文件不存在"}, status_code=404)
    os.remove(fpath)
    rel_path = Path(fpath).relative_to(knowledge_root).as_posix()
    inferred_tier = rel_path.split("/", 1)[0] if "/" in rel_path else "permanent"
    if inferred_tier not in _knowledge_tiers():
        inferred_tier = "permanent"
    delete_knowledge_file_meta(tenant["tenant_id"], inferred_tier, rel_path, tenant.get("tenant_name", ""))
    return {"ok": True, "msg": f"已删除 {file}"}


@app.get("/api/tenant/chat-logs")
async def tenant_chat_logs(
    request: Request,
    page: int = 1,
    phone: str = "",
    agent_id: str = "",
    request_id: str = "",
    q: str = "",
):
    """租户后台：查看本租户聊天日志。"""
    tenant = verify_tenant_admin_request(request)
    logs, total = list_chat_logs(
        page=page,
        per_page=20,
        phone=phone,
        tenant_id=tenant["tenant_id"],
        agent_id=agent_id.strip() or None,
        request_id=request_id.strip() or None,
        q=q,
    )
    return {"ok": True, "logs": logs, "total": total, "page": page}


@app.get("/api/tenant/users")
async def tenant_users(request: Request, page: int = 1):
    """租户后台：查看当前租户手机号成员账号。"""
    tenant = verify_tenant_admin_request(request)
    items, total = list_tenant_phone_accounts(tenant["tenant_id"], page=page, per_page=50)
    for item in items:
        item["agent_ids"] = list_user_agent_bindings(
            tenant_id=tenant["tenant_id"],
            phone=str(item.get("username") or "").strip(),
        )
    return {"ok": True, "items": items, "total": total, "page": page}


@app.post("/api/tenant/users")
async def tenant_save_user(request: Request):
    """租户后台：创建或更新手机号成员账号。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    phone = str(body.get("username", "")).strip()
    display_name = str(body.get("display_name", "")).strip()
    password = str(body.get("password", "")).strip()
    enabled = bool(body.get("enabled", True))
    agent_ids = body.get("agent_ids") or []
    if not phone:
        return JSONResponse({"ok": False, "msg": "手机号不能为空"}, status_code=400)
    result = save_tenant_phone_account(
        tenant_id=tenant["tenant_id"],
        phone=phone,
        display_name=display_name,
        password=password,
        enabled=enabled,
    )
    status = 200 if result.get("ok") else 400
    if not result.get("ok"):
        return JSONResponse(result, status_code=status)
    if isinstance(agent_ids, list):
        bind_result = save_user_agent_bindings(
            tenant_id=tenant["tenant_id"],
            phone=phone,
            agent_ids=agent_ids,
        )
        if not bind_result.get("ok"):
            return JSONResponse(bind_result, status_code=400)
    items, total = list_tenant_phone_accounts(tenant["tenant_id"], page=1, per_page=50)
    for item in items:
        item["agent_ids"] = list_user_agent_bindings(
            tenant_id=tenant["tenant_id"],
            phone=str(item.get("username") or "").strip(),
        )
    return {"ok": True, "msg": result.get("msg", "保存成功"), "items": items, "total": total}


@app.post("/api/tenant/users/toggle")
async def tenant_toggle_user(request: Request):
    """租户后台：启用或停用手机号成员账号。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    phone = str(body.get("username", "")).strip()
    enabled = bool(body.get("enabled", True))
    if not phone:
        return JSONResponse({"ok": False, "msg": "手机号不能为空"}, status_code=400)
    result = toggle_tenant_phone_account(tenant_id=tenant["tenant_id"], phone=phone, enabled=enabled)
    status = 200 if result.get("ok") else 404
    if not result.get("ok"):
        return JSONResponse(result, status_code=status)
    return result


@app.get("/api/tenant/agents")
async def tenant_agents(request: Request):
    """租户后台：查看当前租户智能体列表。"""
    tenant = verify_tenant_admin_request(request)
    return {"ok": True, "items": list_agents(tenant["tenant_id"])}


@app.get("/api/tenant/agents/publish-config")
async def tenant_agent_publish_config(request: Request, agent_id: str = ""):
    """租户后台：读取智能体的发布集成配置。"""
    tenant = verify_tenant_admin_request(request)
    clean_agent_id = str(agent_id or "").strip()
    if not clean_agent_id:
        return JSONResponse({"ok": False, "msg": "智能体ID不能为空"}, status_code=400)
    agent = get_agent(tenant["tenant_id"], clean_agent_id)
    if not agent:
        return JSONResponse({"ok": False, "msg": "智能体不存在"}, status_code=404)
    publish_api_key = ensure_agent_publish_api_key(tenant_id=tenant["tenant_id"], agent_id=clean_agent_id)
    origin = str(request.base_url).rstrip("/")
    chat_url = f"{origin}/chat?tenant_id={tenant['tenant_id']}&agent_id={clean_agent_id}"
    api_base_url = origin
    return {
        "ok": True,
        "agent": {
            "agent_id": clean_agent_id,
            "name": agent.get("name", ""),
            "status": agent.get("status", "draft"),
            "enabled": bool(agent.get("enabled", True)),
        },
        "publish": {
            "chat_url": chat_url,
            "iframe_url": chat_url,
            "api_base_url": api_base_url,
            "api_key": publish_api_key,
            "api_key_masked": _mask_console_api_key(publish_api_key),
            "is_published": str(agent.get("status") or "").strip() == "published",
        },
    }


@app.post("/api/tenant/agents/publish-api-key/regenerate")
async def tenant_regenerate_agent_publish_api_key(request: Request):
    """租户后台：重置智能体的发布 API Key。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    clean_agent_id = str(body.get("agent_id", "") or "").strip()
    if not clean_agent_id:
        return JSONResponse({"ok": False, "msg": "智能体ID不能为空"}, status_code=400)
    try:
        next_key = regenerate_agent_publish_api_key(tenant_id=tenant["tenant_id"], agent_id=clean_agent_id)
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=404)
    return {"ok": True, "agent_id": clean_agent_id, "api_key": next_key, "api_key_masked": _mask_console_api_key(next_key)}


@app.post("/api/tenant/agents")
async def tenant_save_agent(request: Request):
    """租户后台：创建或更新智能体。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    try:
        agent = save_agent(
            tenant_id=tenant["tenant_id"],
            agent_id=body.get("agent_id", ""),
            name=body.get("name", ""),
            description=body.get("description", ""),
            status=body.get("status", "draft"),
            enabled=bool(body.get("enabled", True)),
            avatar=body.get("avatar", ""),
            welcome_message=body.get("welcome_message", ""),
            input_placeholder=body.get("input_placeholder", ""),
            recommended_questions=body.get("recommended_questions") if isinstance(body.get("recommended_questions"), list) else [],
            prompt_override=body.get("prompt_override", ""),
            workflow_id=body.get("workflow_id", ""),
            knowledge_scope=body.get("knowledge_scope") if isinstance(body.get("knowledge_scope"), (dict, list)) else {},
            model_override=(
                body.get("model_override")
                if isinstance(body.get("model_override"), dict)
                else ({"model": str(body.get("model") or "").strip()} if str(body.get("model") or "").strip() else {})
            ),
            tool_scope=body.get("tool_scope") if isinstance(body.get("tool_scope"), list) else [],
            mcp_servers=body.get("mcp_servers") if isinstance(body.get("mcp_servers"), list) else [],
            streaming=bool(body.get("streaming", True)),
            fallback_enabled=bool(body.get("fallback_enabled", True)),
            fallback_message=body.get("fallback_message", ""),
            show_recommended=bool(body.get("show_recommended", True)),
            is_default=bool(body.get("is_default", False)),
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "msg": str(exc)}, status_code=400)
    return {"ok": True, "agent": agent, "items": list_agents(tenant["tenant_id"])}


@app.post("/api/tenant/agents/toggle")
async def tenant_toggle_agent(request: Request):
    """租户后台：启用或停用智能体。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    agent_id = str(body.get("agent_id", "") or "").strip()
    enabled = bool(body.get("enabled", True))
    if not agent_id:
        return JSONResponse({"ok": False, "msg": "智能体ID不能为空"}, status_code=400)
    result = toggle_agent(tenant_id=tenant["tenant_id"], agent_id=agent_id, enabled=enabled)
    if not result.get("ok"):
        return JSONResponse(result, status_code=404)
    return {"ok": True, "msg": result.get("msg", ""), "items": list_agents(tenant["tenant_id"])}


@app.delete("/api/tenant/agents")
async def tenant_delete_agent(request: Request, agent_id: str = ""):
    """租户后台：删除智能体。"""
    tenant = verify_tenant_admin_request(request)
    clean_agent_id = str(agent_id or "").strip()
    if not clean_agent_id:
        return JSONResponse({"ok": False, "msg": "智能体ID不能为空"}, status_code=400)
    result = delete_agent(tenant_id=tenant["tenant_id"], agent_id=clean_agent_id)
    if not result.get("ok"):
        return JSONResponse(result, status_code=404)
    return {"ok": True, "msg": result.get("msg", ""), "items": list_agents(tenant["tenant_id"])}


@app.get("/api/tenant/users/agent-bindings")
async def tenant_user_agent_bindings(request: Request, username: str = ""):
    """租户后台：查看某个成员账号绑定的智能体。"""
    tenant = verify_tenant_admin_request(request)
    phone = str(username or "").strip()
    if not phone:
        return JSONResponse({"ok": False, "msg": "成员账号不能为空"}, status_code=400)
    return {
        "ok": True,
        "username": phone,
        "agent_ids": list_user_agent_bindings(tenant_id=tenant["tenant_id"], phone=phone),
    }


@app.post("/api/tenant/users/agent-bindings")
async def tenant_save_user_agent_bindings(request: Request):
    """租户后台：更新成员账号可访问的智能体。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    phone = str(body.get("username", "") or "").strip()
    agent_ids = body.get("agent_ids") or []
    if not phone:
        return JSONResponse({"ok": False, "msg": "成员账号不能为空"}, status_code=400)
    if not isinstance(agent_ids, list):
        return JSONResponse({"ok": False, "msg": "agent_ids 必须为数组"}, status_code=400)
    result = save_user_agent_bindings(
        tenant_id=tenant["tenant_id"],
        phone=phone,
        agent_ids=agent_ids,
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@app.get("/api/tenant/evaluations")
async def tenant_evaluations(request: Request, page: int = 1):
    """租户后台：查看本租户检索评测历史。"""
    tenant = verify_tenant_admin_request(request)
    logs, total = list_evaluation_runs(page=page, per_page=20, tenant_id=tenant["tenant_id"])
    return {"ok": True, "logs": logs, "total": total, "page": page}


@app.post("/api/tenant/evaluations/run")
async def tenant_run_evaluations(request: Request):
    """租户后台：运行本租户检索评测。"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    cases = body.get("cases") or []
    if not isinstance(cases, list) or not cases:
        return JSONResponse({"ok": False, "msg": "请提供评测题集 cases"}, status_code=400)
    name = str(body.get("name") or "租户检索评测").strip() or "租户检索评测"
    backend_override = str(body.get("backend_override") or "").strip() or None
    runtime = _tenant_runtime_bundle(tenant)
    result = await run_retrieval_evaluation(
        rag_runtime=runtime["rag_runtime"],
        cases=cases,
        retrieval_config=runtime["retrieval_settings"],
        backend_override=backend_override,
        tenant_id=tenant["tenant_id"],
        app_loader=lambda: runtime["app_settings"],
        model_loader=lambda: runtime["model_settings"],
        prompt_loader=lambda: runtime["prompt_template"],
        llm_context={
            "default_base_url": LLM_BASE_URL,
            "ssl_ctx": _ssl_ctx,
            "user_facing_error": user_facing_llm_error,
        },
    )
    record_evaluation_run(
        tenant_id=tenant["tenant_id"],
        name=name,
        total_questions=result["total_questions"],
        hit_at_1=result["hit_at_1"],
        hit_at_3=result["hit_at_3"],
        hit_at_5=result["hit_at_5"],
        avg_top_score=result["avg_top_score"],
        detail=result.get("detail") or [],
        config_snapshot=result.get("config_snapshot") or {},
    )
    return {"ok": True, "result": result}


@app.get("/api/tenant/request-logs")
async def tenant_request_logs(request: Request, page: int = 1):
    """租户后台：查看本租户请求日志。"""
    tenant = verify_tenant_admin_request(request)
    logs, total = list_request_logs(page=page, per_page=20, tenant_id=tenant["tenant_id"])
    return {"ok": True, "logs": logs, "total": total, "page": page}


@app.get("/api/tenant/guardrail-events")
async def tenant_guardrail_events(request: Request, page: int = 1):
    """租户后台：查看本租户护栏事件。"""
    tenant = verify_tenant_admin_request(request)
    logs, total = list_guardrail_events(page=page, per_page=20, tenant_id=tenant["tenant_id"])
    return {"ok": True, "logs": logs, "total": total, "page": page}


@app.get("/api/tenant/observability/summary")
async def tenant_observability_summary(request: Request):
    """租户后台：查看本租户观测汇总。"""
    tenant = verify_tenant_admin_request(request)
    return {
        "ok": True,
        "summary": get_observability_summary(tenant_id=tenant["tenant_id"]),
        "concurrency": get_concurrency_snapshot(),
    }


@app.get("/api/tenant/analytics/summary")
async def tenant_analytics_summary(request: Request, days: int = 7):
    """租户后台：统计报表概览数据"""
    tenant = verify_tenant_admin_request(request)
    data = get_tenant_analytics_summary(tenant["tenant_id"], days=days)
    return {"ok": True, "data": data}


@app.get("/api/tenant/analytics/daily-trends")
async def tenant_analytics_daily_trends(request: Request, days: int = 7):
    """租户后台：每日趋势数据"""
    tenant = verify_tenant_admin_request(request)
    data = get_tenant_daily_trends(tenant["tenant_id"], days=days)
    return {"ok": True, "data": data}


@app.get("/api/tenant/analytics/agent-usage")
async def tenant_analytics_agent_usage(request: Request, days: int = 7, limit: int = 10):
    """租户后台：智能体使用情况"""
    tenant = verify_tenant_admin_request(request)
    data = get_tenant_agent_usage(tenant["tenant_id"], days=days, limit=limit)
    return {"ok": True, "data": data}


@app.get("/api/tenant/analytics/top-questions")
async def tenant_analytics_top_questions(request: Request, days: int = 7, limit: int = 10):
    """租户后台：热门问题"""
    tenant = verify_tenant_admin_request(request)
    data = get_tenant_top_questions(tenant["tenant_id"], days=days, limit=limit)
    return {"ok": True, "data": data}


@app.get("/api/tenant/analytics/active-users")
async def tenant_analytics_active_users(request: Request, days: int = 7, limit: int = 10):
    """租户后台：活跃用户排行"""
    tenant = verify_tenant_admin_request(request)
    data = get_tenant_active_users(tenant["tenant_id"], days=days, limit=limit)
    return {"ok": True, "data": data}


@app.get("/api/tenant/analytics/hourly-distribution")
async def tenant_analytics_hourly_distribution(request: Request, days: int = 7):
    """租户后台：时段分布"""
    tenant = verify_tenant_admin_request(request)
    data = get_tenant_hourly_distribution(tenant["tenant_id"], days=days)
    return {"ok": True, "data": data}


@app.get("/api/tenant/analytics/annotations")
async def tenant_analytics_annotations(request: Request, days: int = 7):
    """租户后台：标注汇总"""
    tenant = verify_tenant_admin_request(request)
    data = get_chat_annotation_summary(tenant_id=tenant["tenant_id"], days=days)
    return {"ok": True, "data": data}


@app.get("/api/tenant/analytics/annotation-labels")
async def tenant_analytics_annotation_labels(request: Request, days: int = 7, limit: int = 10):
    """租户后台：标注标签分布"""
    tenant = verify_tenant_admin_request(request)
    data = get_chat_annotation_label_distribution(
        tenant_id=tenant["tenant_id"],
        days=days,
        limit=limit,
    )
    return {"ok": True, "data": data}


@app.get("/api/tenant/annotations")
async def tenant_annotations(request: Request, page: int = 1, page_size: int = 20, q: str = ""):
    """租户后台：查看对话标注列表"""
    tenant = verify_tenant_admin_request(request)
    safe_page_size = max(1, min(int(page_size or 20), 100))
    items, total = list_chat_annotations(
        tenant_id=tenant["tenant_id"],
        page=page,
        per_page=safe_page_size,
        q=q,
    )
    return {"ok": True, "items": items, "total": total, "page": page, "page_size": safe_page_size}


@app.post("/api/tenant/annotations")
async def tenant_save_annotation(request: Request):
    """租户后台：创建或更新对话标注"""
    tenant = verify_tenant_admin_request(request)
    body = await request.json()
    chat_log_id = int(body.get("chat_log_id") or 0)
    if chat_log_id <= 0:
        return JSONResponse({"ok": False, "msg": "chat_log_id 无效"}, status_code=400)
    item = save_chat_annotation(
        tenant_id=tenant["tenant_id"],
        chat_log_id=chat_log_id,
        session_id=str(body.get("session_id") or "").strip(),
        request_id=str(body.get("request_id") or "").strip(),
        agent_id=str(body.get("agent_id") or "").strip(),
        phone=str(body.get("phone") or "").strip(),
        label=str(body.get("label") or "").strip(),
        score=int(body.get("score") or 0),
        note=str(body.get("note") or "").strip(),
        created_by=str(tenant.get("admin_username") or ""),
    )
    return {"ok": True, "item": item}


@app.get("/api/admin/analytics/overview")
async def admin_analytics_overview(request: Request, days: int = 7):
    """平台后台：统计报表聚合数据"""
    verify_admin(request)
    safe_days = max(1, min(int(days or 7), 90))
    data = get_platform_analytics_overview(days=safe_days)
    return {"ok": True, "data": data}


@app.put("/api/admin/system-prompt")
async def admin_update_system_prompt(request: Request):
    verify_admin(request)
    body = await request.json()
    content = body.get("content", "")
    if not isinstance(content, str) or not content.strip():
        return JSONResponse({"ok": False, "msg": "提示词不能为空"}, status_code=400)
    if "{knowledge_context}" not in content:
        return JSONResponse(
            {
                "ok": False,
                "msg": "当前提示词缺少知识库内容占位标记，保存后系统将无法引用知识库内容，请保留默认知识注入位置后再保存。",
            },
            status_code=400,
        )
    ensure_system_prompt_file()
    with open(SYSTEM_PROMPT_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    semantic_cache.clear()
    return {
        "ok": True,
        "msg": "系统提示词已保存",
        "path": "data/prompts/system_prompt.md",
        "mtime": os.path.getmtime(SYSTEM_PROMPT_PATH),
        "size": os.path.getsize(SYSTEM_PROMPT_PATH),
    }

@app.put("/api/admin/knowledge/file")
async def admin_update_file(request: Request):
    """Update file content and rebuild index."""
    verify_admin(request)
    body = await request.json()
    filename = body.get("file", "")
    content = body.get("content", "")
    if not filename:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    fpath = _find_knowledge_file_path(_knowledge_dir(), filename)
    if not os.path.isfile(fpath):
        return JSONResponse({"ok": False, "msg": "文件不存在"}, status_code=404)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(content)
    count = rag_engine.build_index()
    semantic_cache.clear()
    return {"ok": True, "msg": f"文件已更新，共 {count} 个知识片段"}

@app.delete("/api/admin/knowledge/file")
async def admin_delete_file(request: Request, tier: str = "", file: str = ""):
    """Delete a knowledge file and rebuild index."""
    verify_admin(request)
    if not file:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    fpath = _find_knowledge_file_path(_knowledge_dir(), file)
    if not os.path.isfile(fpath):
        return JSONResponse({"ok": False, "msg": "文件不存在"}, status_code=404)
    os.remove(fpath)
    count = rag_engine.build_index()
    semantic_cache.clear()
    return {"ok": True, "msg": f"已删除 {file}，剩余 {count} 个知识片段"}

@app.get("/api/admin/knowledge/download")
async def admin_download_file(request: Request, tier: str = "", file: str = ""):
    """Download a knowledge file."""
    verify_admin(request)
    if not file:
        return JSONResponse({"ok": False, "msg": "参数错误"}, status_code=400)
    fpath = _find_knowledge_file_path(_knowledge_dir(), file)
    if not os.path.isfile(fpath):
        return JSONResponse({"ok": False, "msg": "文件不存在"}, status_code=404)
    return FileResponse(fpath, filename=file, media_type="text/markdown")

@app.get("/api/admin/knowledge/template")
async def admin_download_template(request: Request):
    """Download a sample knowledge file template."""
    verify_admin(request)
    template = """# 知识库模板

## 适用主题
- 制度问答 / 产品资料 / SOP / 公告 / 专题攻略

## 关键结论
- 用 1 到 3 条结论概括最重要的信息。

## 正文摘录
- 这里填写高价值正文。
- 关键名词、时间、数值、规则建议用 **加粗** 标记。

## 可回答问题
- 这个流程怎么走
- 这个规则什么时候生效
- 某项功能如何配置

## 标签
Tag: #制度 #SOP #公告

## 注意事项
- 一个文件聚焦一个主题。
- 内容尽量结构化，避免空话。
- 文件请保存为 UTF-8 编码的 .md。
"""
    from fastapi.responses import Response
    return Response(
        content=template,
        media_type="text/markdown",
        headers={"Content-Disposition": "attachment; filename=knowledge_template.md"}
    )

# --- Static Files ---
@app.api_route("/", methods=["GET", "HEAD"])
async def serve_index():
    # 默认前台入口切到 V2 登录页，旧前台文件保留但不再作为主入口。
    fpath = os.path.join(FRONTEND_DIR, "login_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/login", methods=["GET", "HEAD"])
async def serve_login():
    fpath = os.path.join(FRONTEND_DIR, "login_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/chat", methods=["GET", "HEAD"])
async def serve_chat():
    fpath = os.path.join(FRONTEND_DIR, "index_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/admin", methods=["GET", "HEAD"])
async def serve_admin():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html" if _is_single_backend_mode() else "admin_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant", methods=["GET", "HEAD"])
async def serve_tenant():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/admin-v2", methods=["GET", "HEAD"])
async def serve_admin_v2():
    # 企业版直接复用单后台；服务商版使用平台总后台。
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html" if _is_single_backend_mode() else "admin_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant-v2", methods=["GET", "HEAD"])
async def serve_tenant_v2():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/chat-v2", methods=["GET", "HEAD"])
async def serve_chat_v2():
    fpath = os.path.join(FRONTEND_DIR, "index_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/login-v2", methods=["GET", "HEAD"])
async def serve_login_v2():
    fpath = os.path.join(FRONTEND_DIR, "login_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/factory", methods=["GET", "HEAD"])
async def serve_factory_center():
    """母版项目专用：版本打包中心，不进入对外销售版本菜单。"""
    if not load_app_config().get("factory_enabled", True):
        raise HTTPException(status_code=404, detail="Not Found")
    fpath = os.path.join(FRONTEND_DIR, "factory_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/platform-tenants-v2", methods=["GET", "HEAD"])
@app.api_route("/platform/tenants", methods=["GET", "HEAD"])
async def serve_platform_tenants_v2():
    if _is_single_backend_mode():
        raise HTTPException(status_code=404, detail="当前版本未启用平台总后台")
    fpath = os.path.join(FRONTEND_DIR, "admin_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/platform-logs-v2", methods=["GET", "HEAD"])
@app.api_route("/platform/logs", methods=["GET", "HEAD"])
async def serve_platform_logs_v2():
    if _is_single_backend_mode():
        raise HTTPException(status_code=404, detail="当前版本未启用平台总后台")
    fpath = os.path.join(FRONTEND_DIR, "admin_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant-branding-v2", methods=["GET", "HEAD"])
@app.api_route("/tenant/branding", methods=["GET", "HEAD"])
async def serve_tenant_branding_v2():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant-knowledge-v2", methods=["GET", "HEAD"])
@app.api_route("/tenant/knowledge", methods=["GET", "HEAD"])
async def serve_tenant_knowledge_v2():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant-users-v2", methods=["GET", "HEAD"])
@app.api_route("/tenant/users", methods=["GET", "HEAD"])
async def serve_tenant_users_v2():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant-crawler-v2", methods=["GET", "HEAD"])
@app.api_route("/tenant/crawler", methods=["GET", "HEAD"])
async def serve_tenant_crawler_v2():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant-model-v2", methods=["GET", "HEAD"])
@app.api_route("/tenant/model", methods=["GET", "HEAD"])
@app.api_route("/tenant/mcp", methods=["GET", "HEAD"])
async def serve_tenant_model_v2():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant-logs-v2", methods=["GET", "HEAD"])
@app.api_route("/tenant/logs", methods=["GET", "HEAD"])
async def serve_tenant_logs_v2():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant-qa-v2", methods=["GET", "HEAD"])
@app.api_route("/tenant/qa", methods=["GET", "HEAD"])
async def serve_tenant_qa_v2():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant/workflow", methods=["GET", "HEAD"])
async def serve_tenant_workflow():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant/agents", methods=["GET", "HEAD"])
async def serve_tenant_agents():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant/analytics", methods=["GET", "HEAD"])
async def serve_tenant_analytics():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/platform/analytics", methods=["GET", "HEAD"])
async def serve_platform_analytics():
    fpath = os.path.join(FRONTEND_DIR, "admin_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/platform_analytics_v2.html", methods=["GET", "HEAD"])
async def serve_platform_analytics_v2():
    fpath = os.path.join(FRONTEND_DIR, "platform_analytics_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/tenant/workflow/editor", methods=["GET", "HEAD"])
async def serve_tenant_workflow_editor():
    fpath = os.path.join(FRONTEND_DIR, "tenant_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

@app.api_route("/analytics_v2.html", methods=["GET", "HEAD"])
async def serve_analytics_v2():
    fpath = os.path.join(FRONTEND_DIR, "analytics_v2.html")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    return HTMLResponse(content, headers={
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
    })

# Serve any static files from frontend dir
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("backend.main:app", host=HOST, port=PORT, reload=False)
