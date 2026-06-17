"""通用采集执行器。

这层不依赖旧的游戏专题采集逻辑，专门服务企业版后台的 Python 脚本设置。
当前先支持：
- 网站页面
- JSON API

后续如果要接数据库，可在这里继续扩展。
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from backend.app_config import get_knowledge_tiers
from backend.config import INGEST_OUTPUT_PREFIX, INGEST_USER_AGENT
from backend.document_processing import normalize_tier


@dataclass
class GenericCrawlerResult:
    """通用采集执行结果。"""

    ok: bool
    source_id: str
    source_name: str
    tier: str
    items_count: int
    output_file: str
    title: str
    message: str


class GenericCrawlerError(RuntimeError):
    """面向前台的抓取错误。"""


def _clean_text(text: str) -> str:
    text = re.sub(r"\r\n?", "\n", text or "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\u3000", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _safe_filename(source_id: str) -> str:
    clean = re.sub(r"[^a-zA-Z0-9_-]+", "_", source_id or "source").strip("_")
    return f"{INGEST_OUTPUT_PREFIX}{clean or 'source'}.md"


def _parse_rule_text(rule_text: str) -> dict[str, object]:
    """解析后台填写的抓取规则。"""
    config: dict[str, object] = {}
    for raw_line in (rule_text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().lower()
        value = value.strip()
        if not value:
            continue
        if key in {"include", "exclude", "selector"}:
            config[key] = [item.strip() for item in re.split(r"[|,]", value) if item.strip()]
        elif key == "limit":
            try:
                config[key] = max(1, min(int(value), 200))
            except ValueError:
                continue
    return config


def _strip_noise_nodes(soup: BeautifulSoup) -> BeautifulSoup:
    """尽量剔除正文外的噪音节点。"""
    selectors = [
        "script",
        "style",
        "noscript",
        "header",
        "footer",
        "nav",
        "aside",
        ".sidebar",
        ".comment",
        ".comments",
        ".recommend",
        ".related",
        ".ad",
        ".ads",
        "[role='navigation']",
    ]
    for selector in selectors:
        for node in soup.select(selector):
            node.decompose()
    return soup


def _extract_html_lines_by_selector(html: str, selectors: list[str], limit: int) -> list[str]:
    soup = _strip_noise_nodes(BeautifulSoup(html, "html.parser"))
    lines: list[str] = []
    for selector in selectors:
        try:
            nodes = soup.select(selector)
        except Exception:
            continue
        for node in nodes:
            text = _clean_text(node.get_text(" ", strip=True))
            if not text:
                continue
            prefix = "- "
            if node.name == "h1":
                prefix = "# "
            elif node.name == "h2":
                prefix = "## "
            elif node.name == "h3":
                prefix = "### "
            lines.append(prefix + text)
            if len(lines) >= limit:
                return lines
    return lines[:limit]


def _node_to_line(node) -> str:
    text = _clean_text(node.get_text(" ", strip=True))
    if not text:
        return ""
    prefix = "- "
    if node.name == "h1":
        prefix = "# "
    elif node.name == "h2":
        prefix = "## "
    elif node.name == "h3":
        prefix = "### "
    return prefix + text


def _content_block_score(node) -> float:
    text = _clean_text(node.get_text(" ", strip=True))
    if not text:
        return 0.0
    score = float(min(len(text), 400))
    paragraphs = len(node.find_all(["p", "li"]))
    headings = len(node.find_all(["h1", "h2", "h3"]))
    score += paragraphs * 35 + headings * 20
    class_hint = " ".join(node.get("class", [])) if node.get("class") else ""
    node_id = str(node.get("id") or "")
    hint_text = f"{class_hint} {node_id}".lower()
    if any(word in hint_text for word in ["article", "content", "main", "detail", "post", "entry", "正文"]):
        score += 180
    if any(word in hint_text for word in ["footer", "header", "nav", "menu", "sidebar", "recommend", "related", "comment", "ad"]):
        score -= 220
    return score


def _extract_lines_from_best_block(soup: BeautifulSoup, limit: int) -> list[str]:
    candidates = soup.select("article, main, .article, .content, .main, .detail, .post, .entry, [role='main']")
    if not candidates:
        candidates = soup.find_all(["article", "main", "section", "div"])
    best_node = None
    best_score = 0.0
    for node in candidates:
        score = _content_block_score(node)
        if score > best_score:
            best_node = node
            best_score = score
    if best_node is None:
        return []
    lines: list[str] = []
    for node in best_node.find_all(["h1", "h2", "h3", "p", "li"]):
        line = _node_to_line(node)
        if not line:
            continue
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _extract_html_lines(html: str, limit: int) -> tuple[str, list[str]]:
    """按通用正文节点提取文字。"""
    soup = _strip_noise_nodes(BeautifulSoup(html, "html.parser"))
    title = _clean_text((soup.title.get_text(" ", strip=True) if soup.title else "") or "未命名页面")
    preferred_lines = _extract_lines_from_best_block(soup, limit)
    if preferred_lines:
        return title, preferred_lines
    nodes = soup.select("article h1, article h2, article h3, article p, main h1, main h2, main h3, main p, .article h1, .article h2, .article h3, .article p, .content h1, .content h2, .content h3, .content p, h1, h2, h3, p, li")
    lines: list[str] = []
    for node in nodes:
        line = _node_to_line(node)
        if not line:
            continue
        lines.append(line)
        if len(lines) >= limit:
            break
    return title, lines


def _extract_api_lines(text: str, limit: int) -> tuple[str, list[str]]:
    """API 返回内容转成可检索文本。"""
    try:
        data = json.loads(text)
    except Exception:
        clean = [line.strip() for line in text.splitlines() if line.strip()]
        return "API 返回结果", [f"- {line}" for line in clean[:limit]]

    if isinstance(data, dict):
        title = str(data.get("title") or data.get("name") or "API 返回结果").strip() or "API 返回结果"
        lines: list[str] = []
        for key, value in data.items():
            if isinstance(value, (dict, list)):
                value_text = json.dumps(value, ensure_ascii=False)
            else:
                value_text = str(value)
            lines.append(f"- {key}: {value_text}")
            if len(lines) >= limit:
                break
        return title, lines
    if isinstance(data, list):
        lines = []
        for item in data[:limit]:
            if isinstance(item, (dict, list)):
                lines.append(f"- {json.dumps(item, ensure_ascii=False)}")
            else:
                lines.append(f"- {item}")
        return "API 列表结果", lines
    return "API 返回结果", [f"- {data}"]


def _decode_response_text(response: requests.Response) -> str:
    """按响应头、apparent_encoding、utf-8 顺序解码正文，尽量避免中文乱码。"""
    content = response.content or b""
    if not content:
        return ""
    header_encoding = ""
    content_type = str(response.headers.get("content-type") or "")
    match = re.search(r"charset=([^\s;]+)", content_type, flags=re.IGNORECASE)
    if match:
        header_encoding = match.group(1).strip("\"' ")
    candidates: list[str] = []
    for encoding in (header_encoding, getattr(response, "apparent_encoding", "") or "", "utf-8"):
        clean = str(encoding or "").strip()
        if clean and clean.lower() not in {item.lower() for item in candidates}:
            candidates.append(clean)
    for encoding in candidates:
        try:
            return content.decode(encoding)
        except Exception:
            continue
    return content.decode("utf-8", errors="ignore")


def _apply_rules(lines: list[str], html: str, rule_text: str, default_limit: int = 40) -> list[str]:
    rules = _parse_rule_text(rule_text)
    original_lines = list(lines)
    selected = list(lines)
    selectors = rules.get("selector")
    if isinstance(selectors, list) and selectors and html:
        selector_lines = _extract_html_lines_by_selector(html, selectors, default_limit)
        if selector_lines:
            selected = selector_lines
    include_words = rules.get("include")
    if isinstance(include_words, list) and include_words:
        selected = [line for line in selected if any(word in line for word in include_words)]
    exclude_words = rules.get("exclude")
    if isinstance(exclude_words, list) and exclude_words:
        selected = [line for line in selected if not any(word in line for word in exclude_words)]
    if not selected and original_lines:
        selected = original_lines
    limit = rules.get("limit")
    if isinstance(limit, int) and limit > 0:
        return selected[:limit]
    return selected[:default_limit]


def _build_markdown(source: dict, title: str, lines: list[str]) -> str:
    """拼装入库 Markdown。"""
    questions = [str(item).strip() for item in source.get("questions", []) if str(item).strip()]
    notes = str(source.get("notes") or "").strip()
    library_label = str(source.get("library_name") or source.get("library_id") or "默认知识库").strip() or "默认知识库"
    category_label = str(source.get("category_name") or source.get("category_id") or "未分类").strip() or "未分类"
    tags = [
        "#采集",
        f"#{str(source.get('source_type') or 'web')}",
        f"#{library_label}",
        f"#{category_label}",
    ]
    body = [
        "# " + (title or source.get("name") or "未命名页面"),
        "",
        "## 来源信息",
        f"- 来源名称：{source.get('name', '')}",
        f"- 来源地址：{source.get('url', '')}",
        f"- 来源类型：{source.get('source_type', 'web')}",
        f"- 目标知识库：{library_label}",
        f"- 目标分类：{category_label}",
    ]
    if notes:
        body.append(f"- 备注：{notes}")
    if questions:
        body.extend(["", "## 可回答问题"])
        body.extend([f"- {question}" for question in questions])
    body.extend(["", "## 正文提要"])
    if lines:
        body.extend(lines)
    else:
        body.append("- 本次未抽取到正文内容，请检查页面规则。")
    body.extend(
        [
            "",
            f"Tag: {' '.join(tags)}",
            f"搜索关键词：{source.get('name', '')}，{source.get('source_id', '')}，{library_label}，{category_label}，正文，规则抽取",
        ]
    )
    return "\n".join(body).strip() + "\n"


def _user_facing_fetch_error(exc: Exception, url: str) -> str:
    message = str(exc).strip()
    if isinstance(exc, requests.exceptions.SSLError):
        return f"页面访问失败：目标站点 SSL/TLS 握手失败，当前无法建立安全连接。URL: {url}"
    if isinstance(exc, requests.exceptions.Timeout):
        return f"页面访问失败：目标站点响应超时，请稍后重试。URL: {url}"
    if isinstance(exc, requests.exceptions.TooManyRedirects):
        return f"页面访问失败：目标站点重定向次数过多。URL: {url}"
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code == 403:
            return f"页面访问失败：目标站点拒绝访问（403），可能存在反爬或权限限制。URL: {url}"
        if status_code == 404:
            return f"页面访问失败：目标页面不存在（404）。URL: {url}"
        if status_code and 500 <= int(status_code) < 600:
            return f"页面访问失败：目标站点服务异常（{status_code}）。URL: {url}"
        if status_code:
            return f"页面访问失败：目标站点返回异常状态码（{status_code}）。URL: {url}"
    if isinstance(exc, requests.exceptions.ConnectionError):
        lowered = message.lower()
        if "ssl" in lowered or "tls" in lowered or "handshake" in lowered or "wrong version number" in lowered:
            return f"页面访问失败：目标站点 SSL/TLS 握手失败，当前无法建立安全连接。URL: {url}"
        return f"页面访问失败：无法连接到目标站点，请检查地址是否可访问。URL: {url}"
    if isinstance(exc, requests.exceptions.RequestException):
        return f"页面访问失败：请求目标站点时出现异常。URL: {url}"
    return message or f"页面抓取失败：{url}"


def run_generic_crawler(source: dict, knowledge_root: str) -> GenericCrawlerResult:
    """执行单条通用采集任务并写入知识库。"""
    url = str(source.get("url") or "").strip()
    if not url:
        raise ValueError("缺少数据源地址")

    source_type = str(source.get("source_type") or "web").strip().lower() or "web"
    headers = {"User-Agent": INGEST_USER_AGENT}
    # 关闭环境代理继承，避免本地服务和企业内网地址被系统代理错误接管。
    try:
        with requests.Session() as session:
            session.trust_env = False
            response = session.get(url, headers=headers, timeout=20)
        response.raise_for_status()
    except Exception as exc:
        raise GenericCrawlerError(_user_facing_fetch_error(exc, url)) from exc
    text = _decode_response_text(response)

    if source_type == "api":
        title, lines = _extract_api_lines(text, 60)
        html = ""
    else:
        title, lines = _extract_html_lines(text, 60)
        html = text
    final_lines = _apply_rules(lines, html, str(source.get("rule_text") or ""))
    if not final_lines:
        raise GenericCrawlerError(
            f"正文抽取失败：页面已访问成功，但没有识别出可入库正文。该页面可能是动态渲染页面、正文结构过于复杂，或当前页面主要是导航/列表页。URL: {url}"
        )

    library_id = _safe_filename(str(source.get("library_id") or "kb_default"))
    category_id = _safe_filename(str(source.get("category_id") or "uncategorized"))
    target_dir = Path(knowledge_root) / library_id / category_id
    target_dir.mkdir(parents=True, exist_ok=True)
    output_name = _safe_filename(str(source.get("source_id") or "source"))
    output_path = target_dir / output_name
    output_path.write_text(_build_markdown(source, title, final_lines), encoding="utf-8")

    return GenericCrawlerResult(
        ok=True,
        source_id=str(source.get("source_id") or ""),
        source_name=str(source.get("name") or ""),
        tier="permanent",
        items_count=len(final_lines),
        output_file=str(output_path),
        title=title,
        message="采集完成",
    )
