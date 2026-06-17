"""租户配置与租户后台资源管理。"""
from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

from backend.app_config import DEFAULT_APP_CONFIG


BASE_DIR = Path(__file__).resolve().parent.parent
TENANT_DIR = BASE_DIR / "data" / "tenants"
PLATFORM_MODEL_CONFIG_PATH = BASE_DIR / "data" / "model_config.json"
PLATFORM_RETRIEVAL_CONFIG_PATH = BASE_DIR / "data" / "retrieval_config.json"
PLATFORM_TOOL_CONFIG_PATH = BASE_DIR / "data" / "tool_config.json"
PLATFORM_API_KEYS_PATH = BASE_DIR / "config" / "api_keys.txt"


def _tenant_root(tenant_id: str) -> Path:
    clean = (tenant_id or "default").strip().lower() or "default"
    return TENANT_DIR / clean


def _tenant_config_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "app_config.json"


def _tenant_prompt_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "system_prompt.md"


def _tenant_model_config_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "model_config.json"


def _tenant_api_keys_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "api_keys.txt"


def _tenant_retrieval_config_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "retrieval_config.json"


def _tenant_crawler_config_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "crawler_config.json"


def _tenant_tool_config_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "tool_config.json"


def _tenant_workflow_config_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "workflow_config.json"


def _tenant_knowledge_metadata_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "knowledge_metadata.json"


def _tenant_biz_tool_data_path(tenant_id: str) -> Path:
    return _tenant_root(tenant_id) / "biz_tool_data.json"


def _default_tenant_config(tenant_id: str, tenant_name: str) -> dict:
    cfg = copy.deepcopy(DEFAULT_APP_CONFIG)
    cfg["app_id"] = tenant_id
    # 新租户的企业定制默认保持空白，避免后台一打开就带入模板文案。
    cfg["app_name"] = ""
    cfg["app_subtitle"] = ""
    cfg["chat_title"] = ""
    cfg["chat_tagline"] = ""
    cfg["welcome_message"] = ""
    cfg["logo"] = ""
    cfg["recommended_questions"] = []
    cfg["login_hint"] = ""
    cfg["input_placeholder"] = ""
    cfg["send_button_text"] = ""
    cfg["agent_description"] = ""
    cfg["knowledge_namespace"] = tenant_id
    return cfg


def ensure_tenant_storage(tenant_id: str, tenant_name: str) -> None:
    """确保租户目录、配置和提示词存在。"""
    root = _tenant_root(tenant_id)
    root.mkdir(parents=True, exist_ok=True)

    config_path = _tenant_config_path(tenant_id)
    if not config_path.exists():
        config_path.write_text(
            json.dumps(_default_tenant_config(tenant_id, tenant_name), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    prompt_path = _tenant_prompt_path(tenant_id)
    if not prompt_path.exists():
        prompt_path.write_text(
            "你是企业知识库的租户专属智能助理。\n\n"
            "请优先根据下方知识库内容回答；如果知识库没有明确答案，先说明知识不足，再给出谨慎建议。\n\n"
            "【知识库内容开始】\n{knowledge_context}\n【知识库内容结束】\n",
            encoding="utf-8",
        )

    # 新租户默认继承平台模型与检索配置，保证开箱即用。
    tenant_model_path = _tenant_model_config_path(tenant_id)
    if not tenant_model_path.exists() and PLATFORM_MODEL_CONFIG_PATH.exists():
        shutil.copyfile(PLATFORM_MODEL_CONFIG_PATH, tenant_model_path)

    tenant_api_keys_path = _tenant_api_keys_path(tenant_id)
    if not tenant_api_keys_path.exists() and PLATFORM_API_KEYS_PATH.exists():
        shutil.copyfile(PLATFORM_API_KEYS_PATH, tenant_api_keys_path)

    tenant_retrieval_path = _tenant_retrieval_config_path(tenant_id)
    if not tenant_retrieval_path.exists() and PLATFORM_RETRIEVAL_CONFIG_PATH.exists():
        cloned = json.loads(PLATFORM_RETRIEVAL_CONFIG_PATH.read_text(encoding="utf-8"))
        qdrant_cfg = cloned.setdefault("qdrant", {})
        qdrant_cfg["collection"] = f"tenant_{tenant_id}_knowledge"
        milvus_cfg = cloned.setdefault("milvus", {})
        milvus_cfg["collection"] = f"tenant_{tenant_id}_knowledge"
        tenant_retrieval_path.write_text(json.dumps(cloned, ensure_ascii=False, indent=2), encoding="utf-8")

    tenant_tool_path = _tenant_tool_config_path(tenant_id)
    if not tenant_tool_path.exists() and PLATFORM_TOOL_CONFIG_PATH.exists():
        shutil.copyfile(PLATFORM_TOOL_CONFIG_PATH, tenant_tool_path)

    tenant_workflow_path = _tenant_workflow_config_path(tenant_id)
    if not tenant_workflow_path.exists():
        tenant_workflow_path.write_text(
            json.dumps({"default_workflow_id": "", "items": []}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    tenant_knowledge_meta_path = _tenant_knowledge_metadata_path(tenant_id)
    if not tenant_knowledge_meta_path.exists():
        tenant_knowledge_meta_path.write_text(
            json.dumps({"items": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    tenant_biz_tool_path = _tenant_biz_tool_data_path(tenant_id)
    if not tenant_biz_tool_path.exists():
        tenant_biz_tool_path.write_text(
            json.dumps({"items": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_tenant_app_config(tenant_id: str, tenant_name: str) -> dict:
    """读取租户自己的业务配置。"""
    ensure_tenant_storage(tenant_id, tenant_name)
    config_path = _tenant_config_path(tenant_id)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    base = _default_tenant_config(tenant_id, tenant_name)
    if not isinstance(raw, dict):
        return base
    merged = copy.deepcopy(base)
    merged.update(raw)
    theme = copy.deepcopy(base.get("theme", {}))
    theme.update(raw.get("theme") or {})
    merged["theme"] = theme
    merged["knowledge_namespace"] = tenant_id
    return merged


def save_tenant_app_config(tenant_id: str, tenant_name: str, config_data: dict) -> dict:
    """保存租户自己的业务配置。"""
    if not isinstance(config_data, dict):
        raise ValueError("租户配置必须是 JSON 对象")
    merged = load_tenant_app_config(tenant_id, tenant_name)
    merged.update(config_data)
    theme = copy.deepcopy(merged.get("theme", {}))
    theme.update(config_data.get("theme") or {})
    merged["theme"] = theme
    merged["knowledge_namespace"] = tenant_id
    config_path = _tenant_config_path(tenant_id)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def load_tenant_system_prompt(tenant_id: str, tenant_name: str) -> str:
    """读取租户自己的系统提示词。"""
    ensure_tenant_storage(tenant_id, tenant_name)
    return _tenant_prompt_path(tenant_id).read_text(encoding="utf-8")


def save_tenant_system_prompt(tenant_id: str, tenant_name: str, content: str) -> None:
    """保存租户自己的系统提示词。"""
    if "{knowledge_context}" not in content:
        raise ValueError("提示词必须包含 {knowledge_context} 占位符")
    ensure_tenant_storage(tenant_id, tenant_name)
    _tenant_prompt_path(tenant_id).write_text(content, encoding="utf-8")


def get_tenant_paths(tenant_id: str) -> dict:
    """输出租户配置相关路径，供后台显示。"""
    return {
        "root": str(_tenant_root(tenant_id)),
        "config": str(_tenant_config_path(tenant_id)),
        "prompt": str(_tenant_prompt_path(tenant_id)),
        "model_config": str(_tenant_model_config_path(tenant_id)),
        "api_keys": str(_tenant_api_keys_path(tenant_id)),
        "retrieval_config": str(_tenant_retrieval_config_path(tenant_id)),
        "crawler_config": str(_tenant_crawler_config_path(tenant_id)),
        "tool_config": str(_tenant_tool_config_path(tenant_id)),
        "workflow_config": str(_tenant_workflow_config_path(tenant_id)),
    }


def get_tenant_knowledge_dir(tenant_id: str) -> str:
    """返回租户自己的知识空间目录。"""
    clean = (tenant_id or "default").strip().lower() or "default"
    return str(BASE_DIR / "knowledge" / clean)


def get_tenant_model_config_path(tenant_id: str) -> Path:
    """返回租户自己的模型配置路径。"""
    return _tenant_model_config_path(tenant_id)


def get_tenant_api_keys_path(tenant_id: str) -> Path:
    """返回租户自己的 Key 池路径。"""
    return _tenant_api_keys_path(tenant_id)


def get_tenant_retrieval_config_path(tenant_id: str) -> Path:
    """返回租户自己的检索配置路径。"""
    return _tenant_retrieval_config_path(tenant_id)


def get_tenant_crawler_config_path(tenant_id: str) -> Path:
    """返回租户自己的脚本配置路径。"""
    return _tenant_crawler_config_path(tenant_id)


def get_tenant_tool_config_path(tenant_id: str) -> Path:
    """返回租户自己的工具配置路径。"""
    return _tenant_tool_config_path(tenant_id)


def get_tenant_workflow_config_path(tenant_id: str) -> Path:
    """返回租户自己的工作流配置路径。"""
    return _tenant_workflow_config_path(tenant_id)


def get_tenant_knowledge_metadata_path(tenant_id: str) -> Path:
    """返回租户知识资产元数据路径。"""
    return _tenant_knowledge_metadata_path(tenant_id)


def get_tenant_biz_tool_data_path(tenant_id: str) -> Path:
    """返回租户业务工具快照数据路径。"""
    return _tenant_biz_tool_data_path(tenant_id)
