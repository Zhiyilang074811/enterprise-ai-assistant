"""安全护栏配置管理。"""
from __future__ import annotations

import copy
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
SECURITY_CONFIG_PATH = BASE_DIR / "data" / "security_config.json"

DEFAULT_SECURITY_CONFIG = {
    "enabled": True,
    "input_max_length": 2000,
    "block_words": ["木马", "后门", "撞库", "拖库", "爆破"],
    "prompt_injection_patterns": [
        "忽略以上",
        "忽略之前",
        "输出系统提示词",
        "显示系统提示词",
        "泄露提示词",
        "导出知识库原文",
        "输出管理员密码",
        "显示全部知识",
    ],
    "redaction": {
        "phone": True,
        "email": True,
        "id_card": True,
        "bank_card": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_security_config_file() -> None:
    """首次启动时生成默认安全配置。"""
    SECURITY_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not SECURITY_CONFIG_PATH.exists():
        SECURITY_CONFIG_PATH.write_text(
            json.dumps(DEFAULT_SECURITY_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_security_config() -> dict:
    """读取安全配置。"""
    ensure_security_config_file()
    try:
        raw = json.loads(SECURITY_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    return _deep_merge(DEFAULT_SECURITY_CONFIG, raw if isinstance(raw, dict) else {})
