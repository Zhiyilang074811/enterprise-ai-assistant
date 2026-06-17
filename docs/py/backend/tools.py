"""企业 Agent 工具执行层。"""
from __future__ import annotations

import json
import os
import re
import shlex
import smtplib
import ssl
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from typing import Any

from backend.tool_config import load_tool_config

CITY_PATTERN = re.compile(r"(?P<city>[\u4e00-\u9fa5A-Za-z]{2,12})(?:今天|今日|明天|后天)?天气")
EMAIL_ADDRESS_PATTERN = re.compile(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")
EMAIL_SUBJECT_PATTERN = re.compile(r"(?:主题|标题)[:：]\s*(?P<subject>.+?)(?:\s+(?:内容|正文)[:：]|$)")
EMAIL_BODY_PATTERN = re.compile(r"(?:内容|正文)[:：]\s*(?P<body>.+)$", re.DOTALL)
MCP_COMMAND_PATTERN = re.compile(
    r"(?:调用MCP|调用工具)\s+(?P<server>[A-Za-z0-9_-]+)[/:](?P<tool>[A-Za-z0-9_.-]+)(?:\s+(?P<payload>\{.*\}))?",
    re.DOTALL,
)


def _tool_allowed(tool_name: str, allowed_tools: list[str] | None = None) -> bool:
    if not allowed_tools:
        return True
    normalized = {str(item or "").strip() for item in allowed_tools if str(item or "").strip()}
    if not normalized:
        return True
    return tool_name in normalized


def detect_tool_intent(question: str, tool_config: dict | None = None, allowed_tools: list[str] | None = None) -> dict[str, Any]:
    """判断当前问题是否应转成工具调用。"""
    text = str(question or "").strip()
    config = tool_config or {}
    weather_cfg = config.get("weather", {}) if isinstance(config, dict) else {}
    email_cfg = config.get("email", {}) if isinstance(config, dict) else {}

    if _tool_allowed("weather", allowed_tools) and weather_cfg.get("enabled") and any(keyword in text for keyword in ("天气", "气温", "下雨", "降雨")):
        return {"matched": True, "tool": "weather", "reason": "命中天气查询关键词"}

    if _tool_allowed("datetime", allowed_tools) and any(keyword in text for keyword in ("今天周几", "今天星期几", "现在几点", "当前时间", "今天几号", "今天日期")):
        return {"matched": True, "tool": "datetime", "reason": "命中时间日期查询关键词"}

    email_keywords = ("发邮件", "发送邮件", "邮件通知", "发一封邮件", "写邮件")
    if _tool_allowed("email", allowed_tools) and email_cfg.get("enabled") and any(keyword in text for keyword in email_keywords):
        return {"matched": True, "tool": "email", "reason": "命中邮件发送关键词"}

    mcp_cfg = config.get("mcp", {}) if isinstance(config, dict) else {}
    if _tool_allowed("mcp", allowed_tools) and mcp_cfg.get("enabled") and MCP_COMMAND_PATTERN.search(text):
        return {"matched": True, "tool": "mcp", "reason": "命中显式 MCP 工具调用指令"}

    return {"matched": False, "tool": "", "reason": ""}


def _extract_city(question: str, default_city: str = "上海") -> str:
    match = CITY_PATTERN.search(question)
    if match:
        return match.group("city")
    return default_city


def _extract_email_payload(question: str) -> dict[str, str]:
    recipients = EMAIL_ADDRESS_PATTERN.findall(question)
    subject_match = EMAIL_SUBJECT_PATTERN.search(question)
    body_match = EMAIL_BODY_PATTERN.search(question)
    return {
        "to": ",".join(dict.fromkeys(recipients)),
        "subject": (subject_match.group("subject").strip() if subject_match else "企业知识助手通知"),
        "body": (body_match.group("body").strip() if body_match else question.strip()),
    }


def run_weather_tool(question: str, tool_config: dict | None = None) -> dict[str, Any]:
    """执行天气查询工具。"""
    config = (tool_config or {}).get("weather", {})
    if not config.get("enabled"):
        return {"ok": False, "tool": "weather", "message": "天气工具未启用"}
    city = _extract_city(question, str(config.get("default_city") or "上海"))
    endpoint = str(config.get("endpoint") or "").strip()
    if not endpoint:
        return {"ok": False, "tool": "weather", "message": "天气工具未配置接口地址"}

    timeout_seconds = int(config.get("timeout_seconds", 8) or 8)
    request_url = endpoint.replace("{city}", urllib.parse.quote(city))
    req = urllib.request.Request(
        request_url,
        headers={
            "User-Agent": "EnterpriseAgent/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    api_key = str(config.get("api_key") or "").strip()
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except Exception as exc:
        return {"ok": False, "tool": "weather", "message": f"天气查询失败：{exc}"}

    summary = raw
    try:
        payload = json.loads(raw)
        current = payload.get("current_condition", [{}])[0] if isinstance(payload, dict) else {}
        desc = ""
        weather_desc = current.get("weatherDesc")
        if isinstance(weather_desc, list) and weather_desc:
            desc = str(weather_desc[0].get("value") or "")
        temp_c = str(current.get("temp_C") or "")
        humidity = str(current.get("humidity") or "")
        feels_like = str(current.get("FeelsLikeC") or "")
        segments = [f"{city}当前天气"]
        if desc:
            segments.append(desc)
        if temp_c:
            segments.append(f"气温 {temp_c}°C")
        if feels_like:
            segments.append(f"体感 {feels_like}°C")
        if humidity:
            segments.append(f"湿度 {humidity}%")
        summary = "，".join(segments)
    except Exception:
        summary = raw[:300]

    return {
        "ok": True,
        "tool": "weather",
        "city": city,
        "message": summary,
        "skip_cache": True,
    }


def run_email_tool(question: str, tool_config: dict | None = None) -> dict[str, Any]:
    """执行邮件发送工具。"""
    config = (tool_config or {}).get("email", {})
    if not config.get("enabled"):
        return {"ok": False, "tool": "email", "message": "邮件工具未启用"}

    payload = _extract_email_payload(question)
    recipients = [item.strip() for item in payload["to"].split(",") if item.strip()]
    if not recipients:
        return {"ok": False, "tool": "email", "message": "未识别到有效收件人邮箱"}

    allow_domains = [str(item).strip().lower() for item in config.get("allow_domains", []) if str(item).strip()]
    if allow_domains:
        invalid = []
        for address in recipients:
            domain = address.split("@")[-1].lower()
            if domain not in allow_domains:
                invalid.append(address)
        if invalid:
            return {"ok": False, "tool": "email", "message": f"以下邮箱域名未在白名单中：{', '.join(invalid)}"}

    smtp_host = str(config.get("smtp_host") or "").strip()
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "").strip()
    from_email = str(config.get("from_email") or username).strip()
    if not smtp_host or not username or not password or not from_email:
        return {"ok": False, "tool": "email", "message": "邮件工具缺少 SMTP 主机、用户名、密码或发件人"}

    msg = EmailMessage()
    from_name = str(config.get("from_name") or "企业知识助手").strip()
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = payload["subject"]
    msg.set_content(payload["body"])

    smtp_port = int(config.get("smtp_port", 465) or 465)
    use_ssl = bool(config.get("use_ssl", True))
    use_tls = bool(config.get("use_tls", False))

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, context=ssl.create_default_context(), timeout=12) as server:
                server.login(username, password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=12) as server:
                if use_tls:
                    server.starttls(context=ssl.create_default_context())
                server.login(username, password)
                server.send_message(msg)
    except Exception as exc:
        return {"ok": False, "tool": "email", "message": f"邮件发送失败：{exc}"}

    return {
        "ok": True,
        "tool": "email",
        "message": f"邮件已发送给 {', '.join(recipients)}，主题：{payload['subject']}",
        "skip_cache": True,
    }


def run_datetime_tool(question: str) -> dict[str, Any]:
    """执行本地时间日期工具。"""
    now = datetime.now()
    question_text = str(question or "")
    weekday_map = {
        0: "星期一",
        1: "星期二",
        2: "星期三",
        3: "星期四",
        4: "星期五",
        5: "星期六",
        6: "星期日",
    }
    if "几点" in question_text or "时间" in question_text:
        message = f"当前本地时间为 {now.strftime('%Y-%m-%d %H:%M:%S')}。"
    elif "周几" in question_text or "星期几" in question_text:
        message = f"今天是 {now.strftime('%Y-%m-%d')}，{weekday_map.get(now.weekday(), '星期未知')}。"
    else:
        message = f"今天日期是 {now.strftime('%Y-%m-%d')}，{weekday_map.get(now.weekday(), '星期未知')}。"
    return {
        "ok": True,
        "tool": "datetime",
        "message": message,
        "skip_cache": True,
    }


def run_mcp_tool(
    question: str,
    tool_config: dict | None = None,
    tenant_id: str | None = None,
    tenant_name: str = "",
    allowed_mcp_servers: list[str] | None = None,
) -> dict[str, Any]:
    """显式调用 MCP 工具。"""
    config = (tool_config or {}).get("mcp", {})
    if not config.get("enabled"):
        return {"ok": False, "tool": "mcp", "message": "MCP 工具未启用"}
    match = MCP_COMMAND_PATTERN.search(str(question or "").strip())
    if not match:
        return {"ok": False, "tool": "mcp", "message": "请使用“调用MCP 服务ID/工具名 {JSON参数}”格式"}
    server_id = str(match.group("server") or "").strip()
    tool_name = str(match.group("tool") or "").strip()
    payload_text = str(match.group("payload") or "").strip()
    normalized_servers = {str(item or "").strip() for item in (allowed_mcp_servers or []) if str(item or "").strip()}
    if normalized_servers and server_id not in normalized_servers:
        return {"ok": False, "tool": "mcp", "message": f"当前智能体无权调用 MCP 服务：{server_id}"}
    payload = {}
    if payload_text:
        try:
            payload = json.loads(payload_text)
        except Exception:
            return {"ok": False, "tool": "mcp", "message": "MCP 参数不是合法 JSON"}
    servers = config.get("servers") if isinstance(config.get("servers"), list) else []
    target = next(
        (
            item for item in servers
            if isinstance(item, dict)
            and item.get("enabled") is not False
            and str(item.get("server_id") or item.get("id") or "").strip() == server_id
        ),
        None,
    )
    if not target:
        return {"ok": False, "tool": "mcp", "message": f"MCP 服务不存在或未启用：{server_id}"}
    transport = str(target.get("transport") or ("http" if (target.get("bridge_url") or target.get("url")) else "stdio")).strip().lower() or "http"
    bridge_url = str(target.get("bridge_url") or target.get("url") or "").strip()
    command = str(target.get("command") or "").strip()
    if transport == "http" and not bridge_url:
        return {"ok": False, "tool": "mcp", "message": f"MCP 服务未配置调用地址：{server_id}"}
    if transport == "stdio" and not command:
        return {"ok": False, "tool": "mcp", "message": f"MCP 服务未配置启动命令：{server_id}"}
    headers = {"Content-Type": "application/json"}
    if isinstance(target.get("headers"), dict):
        headers.update({str(k): str(v) for k, v in target.get("headers", {}).items()})
    auth_token = str(target.get("auth_token") or "").strip()
    if auth_token:
        headers.setdefault("Authorization", f"Bearer {auth_token}")
    request_body = {
        "tool": tool_name,
        "input": payload,
        "context": {
            "tenant_id": tenant_id or "",
            "tenant_name": tenant_name or "",
        },
    }
    timeout_seconds = int(config.get("request_timeout_seconds", 30) or 30)
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
            proc = subprocess.run(
                [command, *args],
                input=json.dumps(request_body, ensure_ascii=False),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
            raw = (proc.stdout or "").strip()
            parsed = json.loads(raw) if raw else {}
            stderr_text = (proc.stderr or "").strip()
            ok = proc.returncode == 0 and not (isinstance(parsed, dict) and parsed.get("ok") is False)
            return {
                "ok": ok,
                "tool": "mcp",
                "server_id": server_id,
                "tool_name": tool_name,
                "message": str(parsed.get("message") or stderr_text or "MCP 工具调用完成") if isinstance(parsed, dict) else (stderr_text or "MCP 工具调用完成"),
                "result": parsed.get("result") if isinstance(parsed, dict) and "result" in parsed else (parsed if parsed else raw),
                "skip_cache": True,
            }
        except Exception as exc:
            return {"ok": False, "tool": "mcp", "message": f"MCP 调用失败：{exc}"}
    request = urllib.request.Request(
        bridge_url,
        data=json.dumps(request_body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            parsed = json.loads(raw) if raw else {}
            ok = int(getattr(resp, "status", 200) or 200) < 400 and not (isinstance(parsed, dict) and parsed.get("ok") is False)
            return {
                "ok": ok,
                "tool": "mcp",
                "server_id": server_id,
                "tool_name": tool_name,
                "message": str(parsed.get("message") or "MCP 工具调用完成") if isinstance(parsed, dict) else "MCP 工具调用完成",
                "result": parsed.get("result") if isinstance(parsed, dict) and "result" in parsed else parsed,
                "skip_cache": True,
            }
    except Exception as exc:
        return {"ok": False, "tool": "mcp", "message": f"MCP 调用失败：{exc}"}


def run_tool_from_question(
    question: str,
    tenant_id: str | None = None,
    tenant_name: str = "",
    allowed_tools: list[str] | None = None,
    allowed_mcp_servers: list[str] | None = None,
) -> dict[str, Any]:
    """根据用户问题自动选择并执行工具。"""
    config = load_tool_config(tenant_id=tenant_id, tenant_name=tenant_name)
    intent = detect_tool_intent(question, config, allowed_tools=allowed_tools)
    if not intent.get("matched"):
        return {"matched": False, "tool": "", "ok": False, "message": ""}
    tool_name = str(intent.get("tool") or "")
    if tool_name == "weather":
        result = run_weather_tool(question, config)
    elif tool_name == "datetime":
        result = run_datetime_tool(question)
    elif tool_name == "email":
        result = run_email_tool(question, config)
    elif tool_name == "mcp":
        result = run_mcp_tool(
            question,
            config,
            tenant_id=tenant_id,
            tenant_name=tenant_name,
            allowed_mcp_servers=allowed_mcp_servers,
        )
    else:
        result = {"ok": False, "tool": tool_name, "message": f"暂不支持工具：{tool_name}"}
    result["matched"] = True
    result["reason"] = intent.get("reason", "")
    return result
