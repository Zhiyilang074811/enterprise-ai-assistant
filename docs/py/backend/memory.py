"""短期记忆构建模块。

基于最近几轮聊天日志，为当前问答拼出一段紧凑的会话记忆。
这里优先保证稳定性和可控性，不引入复杂长链摘要；后续如果要升级，
可以在这个模块里继续接 LangChain 的摘要记忆或长期记忆能力。
"""
from __future__ import annotations

from typing import Any

from backend.database import list_recent_chat_pairs

try:
    from langchain_core.messages import AIMessage, HumanMessage

    LANGCHAIN_MESSAGES_AVAILABLE = True
except Exception:  # pragma: no cover - 依赖未安装时允许回退
    AIMessage = None
    HumanMessage = None
    LANGCHAIN_MESSAGES_AVAILABLE = False


def _normalize_memory_config(app_settings: dict[str, Any] | None) -> dict[str, Any]:
    """读取短期记忆配置，并补齐默认值。"""
    settings = app_settings or {}
    memory_cfg = settings.get("short_term_memory") or {}
    if not isinstance(memory_cfg, dict):
        memory_cfg = {}
    return {
        "enabled": bool(memory_cfg.get("enabled", True)),
        "max_turns": max(1, int(memory_cfg.get("max_turns", 6) or 6)),
        "max_chars": max(200, int(memory_cfg.get("max_chars", 2400) or 2400)),
    }


def _trim_text(text: str, max_chars: int) -> str:
    """限制单条记忆长度，避免提示词无限膨胀。"""
    clean = str(text or "").strip()
    if len(clean) <= max_chars:
        return clean
    return f"{clean[:max_chars].rstrip()}..."


def _render_conversation_memory(pairs: list[tuple[str, str]], max_chars: int) -> str:
    """把最近若干轮对话渲染成提示词里的短期记忆文本。"""
    if not pairs:
        return ""
    blocks: list[str] = []
    per_item_limit = max(80, max_chars // max(len(pairs), 1))
    if LANGCHAIN_MESSAGES_AVAILABLE:
        messages = []
        for question, answer in pairs:
            messages.append(HumanMessage(content=_trim_text(question, per_item_limit)))
            messages.append(AIMessage(content=_trim_text(answer, per_item_limit)))
        for message in messages:
            role = "用户" if message.type == "human" else "助手"
            blocks.append(f"{role}：{message.content}")
    else:
        for question, answer in pairs:
            blocks.append(f"用户：{_trim_text(question, per_item_limit)}")
            blocks.append(f"助手：{_trim_text(answer, per_item_limit)}")
    text = "\n".join(blocks).strip()
    return _trim_text(text, max_chars)


def build_short_term_memory(
    *,
    phone: str,
    tenant_id: str,
    agent_id: str = "",
    session_id: str = "",
    app_settings: dict[str, Any] | None = None,
) -> str:
    """为当前会话构建最近几轮短期记忆。

    这里按手机号 + 租户维度读取最近聊天记录，避免不同企业和不同账号之间串上下文。
    """
    config = _normalize_memory_config(app_settings)
    if not config["enabled"]:
        return ""
    if not str(phone or "").strip():
        return ""
    logs = list_recent_chat_pairs(
        phone=phone,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        limit=config["max_turns"],
    )
    if not logs:
        return ""
    pairs: list[tuple[str, str]] = []
    for item in logs:
        question = str(item.get("question") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if question and answer:
            pairs.append((question, answer))
    return _render_conversation_memory(pairs, config["max_chars"])
