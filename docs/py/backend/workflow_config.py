"""租户工作流配置管理。"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from backend.tenant_config import ensure_tenant_storage, get_tenant_workflow_config_path

BASE_DIR = Path(__file__).resolve().parent.parent
WORKFLOW_CONFIG_PATH = BASE_DIR / "data" / "workflow_config.json"

DEFAULT_WORKFLOW_ITEM = {
    "workflow_id": "main",
    "name": "默认工作流",
    "description": "当前租户主流程。",
    "enabled": True,
    "sort_order": 100,
    "version": "V1.0",
    "status": "draft",
    "updated_at": "",
    "nodes": [],
    "connections": [],
    "app_overrides": {
        "chat_title": "",
        "chat_tagline": "",
        "welcome_message": "",
        "agent_description": "",
        "recommended_questions": [],
        "input_placeholder": "",
        "send_button_text": "",
    },
    "system_prompt": (
        "你是企业知识库的租户专属智能助理。\n\n"
        "请优先根据下方知识库内容回答；如果知识库没有明确答案，先说明知识不足，再给出谨慎建议。\n\n"
        "【知识库内容开始】\n{knowledge_context}\n【知识库内容结束】\n"
    ),
}

DEFAULT_WORKFLOW_CONFIG = {
    "default_workflow_id": "",
    "items": [],
}


def _resolve_workflow_config_path(tenant_id: str | None = None, tenant_name: str = "") -> Path:
    if tenant_id:
        ensure_tenant_storage(tenant_id, tenant_name or tenant_id)
        return get_tenant_workflow_config_path(tenant_id)
    return WORKFLOW_CONFIG_PATH


def _normalize_connections(value: object) -> list[dict]:
    items = value if isinstance(value, list) else []
    normalized: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        from_id = str(item.get("from") or "").strip()
        to_id = str(item.get("to") or "").strip()
        if not from_id or not to_id:
            continue
        normalized.append(
            {
                "id": str(item.get("id") or f"conn_{index + 1}"),
                "from": from_id,
                "to": to_id,
                "label": str(item.get("label") or "").strip(),
            }
        )
    return normalized


def _normalize_nodes(value: object) -> list[dict]:
    items = value if isinstance(value, list) else []
    normalized: list[dict] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("id") or f"node_{index + 1}").strip()
        node_type = str(item.get("type") or "custom").strip() or "custom"
        try:
            x = int(float(item.get("x") or 0))
        except Exception:
            x = 0
        try:
            y = int(float(item.get("y") or 0))
        except Exception:
            y = 0
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        normalized.append(
            {
                "id": node_id,
                "type": node_type,
                "x": x,
                "y": y,
                "data": copy.deepcopy(data),
            }
        )
    return normalized


def _normalize_workflow_item(item: dict, index: int) -> dict:
    base = copy.deepcopy(DEFAULT_WORKFLOW_ITEM)
    if not isinstance(item, dict):
        item = {}
    workflow_id = str(item.get("workflow_id") or item.get("id") or f"wf_{index + 1}").strip() or f"wf_{index + 1}"
    base.update(
        {
            "workflow_id": workflow_id,
            "name": str(item.get("name") or base["name"]).strip() or base["name"],
            "description": str(item.get("description") or "").strip(),
            "enabled": bool(item.get("enabled", True)),
            "sort_order": int(item.get("sort_order") or ((index + 1) * 100)),
            "version": str(item.get("version") or base["version"]).strip() or base["version"],
            "status": str(item.get("status") or base["status"]).strip() or base["status"],
            "updated_at": str(item.get("updated_at") or item.get("updatedAt") or "").strip(),
            "system_prompt": str(item.get("system_prompt") or base["system_prompt"]).strip() or base["system_prompt"],
            "nodes": _normalize_nodes(item.get("nodes")),
            "connections": _normalize_connections(item.get("connections")),
        }
    )
    overrides = copy.deepcopy(base["app_overrides"])
    overrides.update(item.get("app_overrides") if isinstance(item.get("app_overrides"), dict) else {})
    base["app_overrides"] = overrides
    return base


def ensure_workflow_config_file(tenant_id: str | None = None, tenant_name: str = "") -> None:
    config_path = _resolve_workflow_config_path(tenant_id, tenant_name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(
            json.dumps(DEFAULT_WORKFLOW_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_workflow_config(tenant_id: str | None = None, tenant_name: str = "") -> dict:
    ensure_workflow_config_file(tenant_id, tenant_name)
    config_path = _resolve_workflow_config_path(tenant_id, tenant_name)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    items = raw.get("items") if isinstance(raw, dict) else []
    normalized_items = [_normalize_workflow_item(item, index) for index, item in enumerate(items or [])]
    default_workflow_id = str((raw.get("default_workflow_id") if isinstance(raw, dict) else "") or "").strip()
    if default_workflow_id and not any(item["workflow_id"] == default_workflow_id for item in normalized_items):
        default_workflow_id = ""
    return {
        "default_workflow_id": default_workflow_id,
        "items": normalized_items,
    }


def save_workflow_config(config_data: dict, tenant_id: str | None = None, tenant_name: str = "") -> dict:
    if not isinstance(config_data, dict):
        raise ValueError("工作流配置必须是 JSON 对象")
    items = config_data.get("items")
    if not isinstance(items, list):
        raise ValueError("工作流列表必须是数组")
    normalized_items = [_normalize_workflow_item(item, index) for index, item in enumerate(items)]
    default_workflow_id = str(config_data.get("default_workflow_id") or "").strip()
    if default_workflow_id and not any(item["workflow_id"] == default_workflow_id for item in normalized_items):
        default_workflow_id = ""
    config_path = _resolve_workflow_config_path(tenant_id, tenant_name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    stored = {
        "default_workflow_id": default_workflow_id,
        "items": normalized_items,
        "updated_at": int(time.time()),
    }
    config_path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "default_workflow_id": default_workflow_id,
        "items": normalized_items,
    }
