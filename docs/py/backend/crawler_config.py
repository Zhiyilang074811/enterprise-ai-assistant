"""采集源配置管理。"""
from __future__ import annotations

import json
import os
from copy import deepcopy

from backend.config import PLATFORM_CRAWLER_CONFIG_PATH
from backend.tenant_config import ensure_tenant_storage, get_tenant_crawler_config_path


DEFAULT_CRAWLER_SOURCES: list[dict] = []


def _resolve_crawler_config_path(tenant_id: str | None = None, tenant_name: str = "") -> str:
    """解析采集源配置路径。"""
    if tenant_id:
        ensure_tenant_storage(tenant_id, tenant_name or tenant_id)
        return str(get_tenant_crawler_config_path(tenant_id))
    return PLATFORM_CRAWLER_CONFIG_PATH


def ensure_crawler_config_file(tenant_id: str | None = None, tenant_name: str = "") -> None:
    """确保采集源配置文件存在。"""
    config_path = _resolve_crawler_config_path(tenant_id, tenant_name)
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    if not os.path.exists(config_path):
      with open(config_path, "w", encoding="utf-8") as f:
          json.dump(DEFAULT_CRAWLER_SOURCES, f, ensure_ascii=False, indent=2)


def load_crawler_sources(tenant_id: str | None = None, tenant_name: str = "") -> list[dict]:
    """读取采集源配置。"""
    ensure_crawler_config_file(tenant_id, tenant_name)
    config_path = _resolve_crawler_config_path(tenant_id, tenant_name)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = []
    if not isinstance(data, list):
        return deepcopy(DEFAULT_CRAWLER_SOURCES)
    clean_items: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id", "")).strip()
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if not source_id or not name or not url:
            continue
        refresh_hours = max(1, int(item.get("refresh_hours", 24) or 24))
        frequency = str(item.get("frequency") or "").strip()
        if not frequency:
            frequency = "weekly" if refresh_hours >= 168 else "daily" if refresh_hours >= 24 else "hourly"
        if frequency not in {"once", "hourly", "daily", "weekly"}:
            frequency = "weekly" if refresh_hours >= 168 else "daily" if refresh_hours >= 24 else "hourly"
        clean_items.append(
            {
                "source_id": source_id,
                "name": name,
                "url": url,
                "tier": "permanent",
                "library_id": str(item.get("library_id", "")).strip(),
                "category_id": str(item.get("category_id", "")).strip(),
                "source_type": "web",
                "confidence": str(item.get("confidence", "B")).strip().upper() or "B",
                "parser": str(item.get("parser", "generic")).strip() or "generic",
                "refresh_hours": refresh_hours,
                "frequency": frequency,
                "auto_ingest": bool(item.get("auto_ingest", True)),
                "manual_review": bool(item.get("manual_review", False)),
                "required_any_keywords": [
                    str(keyword).strip()
                    for keyword in item.get("required_any_keywords", [])
                    if str(keyword).strip()
                ],
                "rule_text": "",
                "notes": str(item.get("notes", "")).strip(),
                "questions": [
                    str(question).strip()
                    for question in item.get("questions", [])
                    if str(question).strip()
                ],
            }
        )
    return clean_items


def save_crawler_sources(items: list[dict], tenant_id: str | None = None, tenant_name: str = "") -> list[dict]:
    """保存采集源配置。"""
    if not isinstance(items, list):
        raise ValueError("采集源配置必须是数组")
    normalized = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        source_id = str(item.get("source_id", "")).strip()
        name = str(item.get("name", "")).strip()
        url = str(item.get("url", "")).strip()
        if not source_id or not name or not url:
            raise ValueError("每个采集源都必须填写来源ID、名称和地址")
        if source_id in seen_ids:
            raise ValueError(f"来源ID重复：{source_id}")
        seen_ids.add(source_id)
        refresh_hours = max(1, int(item.get("refresh_hours", 24) or 24))
        frequency = str(item.get("frequency") or "").strip()
        if frequency not in {"once", "hourly", "daily", "weekly"}:
            frequency = "weekly" if refresh_hours >= 168 else "daily" if refresh_hours >= 24 else "hourly"
        normalized.append(
            {
                "source_id": source_id,
                "name": name,
                "url": url,
                "tier": "permanent",
                "library_id": str(item.get("library_id", "")).strip(),
                "category_id": str(item.get("category_id", "")).strip(),
                "source_type": "web",
                "confidence": str(item.get("confidence", "B")).strip().upper() or "B",
                "parser": str(item.get("parser", "generic")).strip() or "generic",
                "refresh_hours": refresh_hours,
                "frequency": frequency,
                "auto_ingest": bool(item.get("auto_ingest", True)),
                "manual_review": bool(item.get("manual_review", False)),
                "required_any_keywords": [
                    str(keyword).strip()
                    for keyword in item.get("required_any_keywords", [])
                    if str(keyword).strip()
                ],
                "rule_text": "",
                "notes": str(item.get("notes", "")).strip(),
                "questions": [
                    str(question).strip()
                    for question in item.get("questions", [])
                    if str(question).strip()
                ],
            }
        )
    config_path = _resolve_crawler_config_path(tenant_id, tenant_name)
    ensure_crawler_config_file(tenant_id, tenant_name)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(normalized, f, ensure_ascii=False, indent=2)
    return normalized
