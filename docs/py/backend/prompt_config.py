"""System Prompt 配置管理。"""
from __future__ import annotations

import os
from pathlib import Path


PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "prompts")
SYSTEM_PROMPT_PATH = os.path.join(PROMPTS_DIR, "system_prompt.md")

DEFAULT_SYSTEM_PROMPT_TEMPLATE = """# Role: 企业知识库智能助理

## Profile
- 你是面向企业与垂类业务的知识库问答 Agent。
- 你的任务是严格依据本地知识库回答问题，并在必要时对信息进行结构化整理。
- 输出要专业、清晰、可信，优先给出结论与可执行建议。

## 核心规则
1. **知识库优先**：
   - 只要知识库中存在相关内容，必须优先依据知识库作答。
   - 如果知识库与模型记忆冲突，以知识库为准。
2. **禁止编造**：
   - 如果知识库没有给出明确事实、数值、时间、规则或结论，必须如实说明“当前知识库未提供该信息”。
   - 严禁补造不存在的流程、制度、坐标、数值、政策或承诺。
3. **适合企业场景**：
   - 输出优先服务于制度问答、SOP 指南、产品资料、专题内容、公告说明与知识检索总结。

## 输出要求
- 先回答结论，再补充依据。
- 信息较多时使用简洁列表。
- 引用知识库时优先采纳与当前问题最匹配的知识库、分类与标签内容。

## 知识库检索结果（RAG）
以下是从知识库中检索到的相关内容：

{knowledge_context}
"""


def ensure_system_prompt_file() -> None:
    """首次启动时补齐默认系统提示词。"""
    os.makedirs(PROMPTS_DIR, exist_ok=True)
    if not os.path.exists(SYSTEM_PROMPT_PATH):
        with open(SYSTEM_PROMPT_PATH, "w", encoding="utf-8") as f:
            f.write(DEFAULT_SYSTEM_PROMPT_TEMPLATE)


def load_system_prompt_template() -> str:
    """读取系统提示词，并保证知识上下文占位符存在。"""
    ensure_system_prompt_file()
    return load_system_prompt_template_from_path(SYSTEM_PROMPT_PATH)


def load_system_prompt_template_from_path(path: str | os.PathLike[str]) -> str:
    """按指定路径读取系统提示词。

    平台总后台和租户后台会使用不同的 Prompt 文件，这里统一做路径级加载，
    避免所有请求都被平台默认提示词绑死。
    """
    prompt_path = Path(path)
    if not prompt_path.exists():
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(DEFAULT_SYSTEM_PROMPT_TEMPLATE, encoding="utf-8")
    content = prompt_path.read_text(encoding="utf-8").strip()
    if "{knowledge_context}" not in content:
        content += "\n\n## 知识库检索结果（RAG）\n\n{knowledge_context}\n"
    return content
