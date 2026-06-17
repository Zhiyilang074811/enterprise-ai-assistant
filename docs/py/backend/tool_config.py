"""工具配置管理。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from backend.tenant_config import ensure_tenant_storage, get_tenant_tool_config_path

BASE_DIR = Path(__file__).resolve().parent.parent
TOOL_CONFIG_PATH = BASE_DIR / "data" / "tool_config.json"

DEFAULT_TOOL_CONFIG = {
    "weather": {
        "enabled": False,
        "provider": "wttr",
        "endpoint": "https://wttr.in/{city}?format=j1",
        "api_key": "",
        "timeout_seconds": 8,
        "default_city": "上海",
    },
    "email": {
        "enabled": False,
        "smtp_host": "",
        "smtp_port": 465,
        "username": "",
        "password": "",
        "from_email": "",
        "from_name": "企业知识助手",
        "use_tls": False,
        "use_ssl": True,
        "allow_domains": [],
    },
    "mcp": {
        "enabled": False,
        "request_timeout_seconds": 30,
        "servers": [],
    },
}


def _resolve_tool_config_path(tenant_id: str | None = None, tenant_name: str = "") -> Path:
    """解析工具配置路径。"""
    if tenant_id:
        ensure_tenant_storage(tenant_id, tenant_name or tenant_id)
        return get_tenant_tool_config_path(tenant_id)
    return TOOL_CONFIG_PATH


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_tool_config_file(tenant_id: str | None = None, tenant_name: str = "") -> None:
    """确保工具配置文件存在。"""
    config_path = _resolve_tool_config_path(tenant_id, tenant_name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(
            json.dumps(DEFAULT_TOOL_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_tool_config(tenant_id: str | None = None, tenant_name: str = "") -> dict:
    """读取工具配置。"""
    ensure_tool_config_file(tenant_id, tenant_name)
    config_path = _resolve_tool_config_path(tenant_id, tenant_name)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    return _deep_merge(DEFAULT_TOOL_CONFIG, raw if isinstance(raw, dict) else {})


def save_tool_config(config_data: dict, tenant_id: str | None = None, tenant_name: str = "") -> dict:
    """保存工具配置。"""
    if not isinstance(config_data, dict):
        raise ValueError("工具配置必须是 JSON 对象")
    merged = _deep_merge(DEFAULT_TOOL_CONFIG, config_data)
    config_path = _resolve_tool_config_path(tenant_id, tenant_name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return merged


def list_enabled_mcp_servers(tool_config: dict | None = None) -> list[dict]:
    """返回启用的 MCP 服务列表。"""
    config = tool_config if isinstance(tool_config, dict) else DEFAULT_TOOL_CONFIG
    mcp_cfg = config.get("mcp") if isinstance(config.get("mcp"), dict) else {}
    enabled = bool(mcp_cfg.get("enabled"))
    servers = mcp_cfg.get("servers") if isinstance(mcp_cfg.get("servers"), list) else []
    if not enabled:
        return []
    result: list[dict] = []
    for item in servers:
        if not isinstance(item, dict):
            continue
        if item.get("enabled") is False:
            continue
        server_id = str(item.get("server_id") or item.get("id") or "").strip()
        if not server_id:
            continue
        result.append(
            {
                "server_id": server_id,
                "label": str(item.get("label") or server_id).strip(),
                "transport": str(item.get("transport") or ("http" if (item.get("bridge_url") or item.get("url")) else "stdio")).strip().lower() or "http",
                "bridge_url": str(item.get("bridge_url") or "").strip(),
                "url": str(item.get("url") or item.get("bridge_url") or "").strip(),
                "command": str(item.get("command") or "").strip(),
                "args": item.get("args") if isinstance(item.get("args"), list) else [],
                "env": item.get("env") if isinstance(item.get("env"), dict) else {},
                "env_passthrough": item.get("env_passthrough") if isinstance(item.get("env_passthrough"), list) else [],
                "auth_token": str(item.get("auth_token") or "").strip(),
                "enabled": True,
                "headers": item.get("headers") if isinstance(item.get("headers"), dict) else {},
            }
        )
    return result
