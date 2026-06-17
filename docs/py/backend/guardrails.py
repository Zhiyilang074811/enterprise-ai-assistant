"""输入输出安全护栏。"""
from __future__ import annotations

import re

from backend.security_config import load_security_config


PHONE_RE = re.compile(r"(?<!\d)(1\d{10})(?!\d)")
EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
ID_CARD_RE = re.compile(r"(?<!\d)(\d{17}[\dXx])(?!\d)")
BANK_CARD_RE = re.compile(r"(?<!\d)(\d{16,19})(?!\d)")


def _mask_middle(text: str, left: int = 3, right: int = 4, fill: str = "*") -> str:
    if len(text) <= left + right:
        return fill * len(text)
    return f"{text[:left]}{fill * max(4, len(text) - left - right)}{text[-right:]}"


def apply_input_guardrails(question: str) -> dict:
    """对输入问题进行长度、敏感词和注入指令检查。"""
    cfg = load_security_config()
    clean = (question or "").strip()
    events: list[dict] = []
    if not cfg.get("enabled", True):
        return {"ok": True, "text": clean, "events": events}

    max_length = int(cfg.get("input_max_length", 2000) or 2000)
    if len(clean) > max_length:
        events.append({"stage": "input", "action": "block", "rule": "input_max_length", "detail": str(max_length)})
        return {"ok": False, "text": clean[:max_length], "events": events, "message": f"问题过长，请控制在 {max_length} 个字符以内"}

    for word in cfg.get("block_words", []) or []:
        if word and word in clean:
            events.append({"stage": "input", "action": "block", "rule": "block_word", "detail": word})
            return {"ok": False, "text": clean, "events": events, "message": "问题触发安全规则，暂不支持处理该请求"}

    lowered = clean.lower()
    for pattern in cfg.get("prompt_injection_patterns", []) or []:
        token = str(pattern or "").strip()
        if token and (token in clean or token.lower() in lowered):
            events.append({"stage": "input", "action": "block", "rule": "prompt_injection", "detail": token})
            return {"ok": False, "text": clean, "events": events, "message": "请求包含高风险指令，已被安全护栏拦截"}

    return {"ok": True, "text": clean, "events": events}


def apply_output_guardrails(text: str) -> tuple[str, list[dict]]:
    """对输出文本做脱敏处理。"""
    cfg = load_security_config()
    clean = text or ""
    events: list[dict] = []
    if not cfg.get("enabled", True):
        return clean, events

    redaction = cfg.get("redaction") or {}
    if redaction.get("phone", True):
        clean, count = PHONE_RE.subn(lambda m: _mask_middle(m.group(1), 3, 4), clean)
        if count:
            events.append({"stage": "output", "action": "mask", "rule": "phone", "detail": str(count)})
    if redaction.get("email", True):
        clean, count = EMAIL_RE.subn(lambda m: _mask_middle(m.group(1), 2, 8), clean)
        if count:
            events.append({"stage": "output", "action": "mask", "rule": "email", "detail": str(count)})
    if redaction.get("id_card", True):
        clean, count = ID_CARD_RE.subn(lambda m: _mask_middle(m.group(1), 4, 4), clean)
        if count:
            events.append({"stage": "output", "action": "mask", "rule": "id_card", "detail": str(count)})
    if redaction.get("bank_card", True):
        clean, count = BANK_CARD_RE.subn(lambda m: _mask_middle(m.group(1), 4, 4), clean)
        if count:
            events.append({"stage": "output", "action": "mask", "rule": "bank_card", "detail": str(count)})
    return clean, events
