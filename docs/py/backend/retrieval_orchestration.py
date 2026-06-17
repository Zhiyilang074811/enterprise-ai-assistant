"""检索调度与纠错模块。

这里负责把“怎么检索”从单纯搜索提升为“按问题类型路由 + 分级重试”：
- query rewrite：轻量改写与关键词扩展
- query profile：判断问题属于编号检索、制度流程、FAQ 还是关键词精确查找
- retrieval route：根据问题类型选择优先检索路径
- retrieval judge：评估召回质量
- retry stages：低质量时按阶段切换策略重试
"""
from __future__ import annotations

import copy
import re
from typing import Any

ENTITY_ALIAS_RULES: list[dict[str, Any]] = [
    {
        "pattern": r"(中国联通|联通|联通RAG|联通知识助手)",
        "canonical": "中国联通",
        "aliases": ["中国联通", "联通", "联通政企", "联通知识助手"],
    },
    {
        "pattern": r"(中国移动|移动|移动RAG|移动知识助手)",
        "canonical": "中国移动",
        "aliases": ["中国移动", "移动", "中国移动政企", "移动知识助手"],
    },
    {
        "pattern": r"(中信戴卡|戴卡)",
        "canonical": "中信戴卡",
        "aliases": ["中信戴卡", "戴卡", "中信戴卡股份有限公司"],
    },
    {
        "pattern": r"(EDI|电子数据交换|报文系统)",
        "canonical": "EDI",
        "aliases": ["EDI", "电子数据交换", "接口报文", "报文系统"],
    },
    {
        "pattern": r"(ERP|SAP)",
        "canonical": "ERP",
        "aliases": ["ERP", "SAP", "企业资源计划"],
    },
    {
        "pattern": r"(RFC|ASN|API|SQL)",
        "canonical": "integration_terms",
        "aliases": ["RFC", "ASN", "API", "SQL", "接口字段", "系统参数"],
    },
]

REALTIME_PATTERNS = (
    "今天", "今天周几", "现在几点", "当前时间", "最近", "最新", "实时", "今日", "明天", "后天",
    "天气", "股价", "汇率", "新闻", "头条", "热搜",
)
GENERAL_PATTERNS = (
    "是什么", "什么意思", "为什么", "如何理解", "介绍一下", "区别", "作用", "概念",
)
KNOWLEDGE_PATTERNS = (
    "制度", "流程", "审批", "规范", "要求", "报销", "入职", "离职", "合同", "发票", "公告",
    "产品", "业务", "专线", "运维", "项目", "SOP", "知识库",
)


def _normalize_retrieval_cfg(config_data: dict[str, Any] | None) -> dict[str, Any]:
    cfg = copy.deepcopy(config_data or {})
    orchestration = cfg.setdefault("orchestration", {})
    rewrite = orchestration.setdefault("rewrite", {})
    retry = orchestration.setdefault("retry", {})
    judge = orchestration.setdefault("judge", {})
    routing = orchestration.setdefault("routing", {})
    rewrite.setdefault("enabled", True)
    rewrite.setdefault("expand_synonyms", True)
    rewrite.setdefault("attempt_expansions", True)
    retry.setdefault("enabled", True)
    retry.setdefault("max_attempts", 2)
    retry.setdefault("fallback_top_k", 8)
    retry.setdefault(
        "stages",
        [
            {"backend": "hybrid", "top_k": 8, "rewrite_mode": "broad"},
            {"backend": "bm25", "top_k": 10, "rewrite_mode": "strict"},
        ],
    )
    judge.setdefault("min_results", 2)
    judge.setdefault("min_top_score", 0.24)
    judge.setdefault("min_avg_score", 0.16)
    routing.setdefault("enabled", True)
    routing.setdefault(
        "profile_backends",
        {
            "identifier_lookup": "bm25",
            "keyword_exact": "hybrid",
            "faq_semantic": "dense",
            "process_policy": "hybrid",
        },
    )
    return cfg


def infer_query_profile(query: str) -> str:
    """根据问题文本推断检索画像。"""
    raw = str(query or "").strip()
    text = raw.lower()
    if not text:
        return "keyword_exact"
    if re.search(r"\b[A-Za-z]{2,8}\b", raw) and re.search(
        r"(系统|接口|平台|报文|字段|参数|目录|路径|SAP|ERP|EDI|RFC|ASN|API|SQL)",
        raw,
        flags=re.IGNORECASE,
    ):
        return "identifier_lookup"
    if re.search(r"\b(api|id|sku|sql|url|http|参数|字段|接口|编号|工号|版本号)\b", text):
        return "identifier_lookup"
    if re.search(r"(制度|流程|审批|报销|入职|离职|合同|发票|规范|SOP|权限)", text, flags=re.IGNORECASE):
        return "process_policy"
    if re.search(r"(是什么|怎么|为何|为什么|如何|说明|介绍|总结|区别)", text):
        return "faq_semantic"
    return "keyword_exact"


def expand_entity_aliases(query: str) -> dict[str, Any]:
    """为企业实体词、系统名和缩写补充别名，提升精确召回。"""
    original = str(query or "").strip()
    if not original:
        return {"rewritten": original, "applied": False, "notes": [], "matched_entities": []}

    rewritten = original
    notes: list[str] = []
    matched_entities: list[str] = []
    for rule in ENTITY_ALIAS_RULES:
        pattern = str(rule.get("pattern") or "")
        aliases = [str(item).strip() for item in (rule.get("aliases") or []) if str(item).strip()]
        canonical = str(rule.get("canonical") or "").strip()
        if not pattern or not aliases:
            continue
        if not re.search(pattern, rewritten, flags=re.IGNORECASE):
            continue
        matched_entities.append(canonical or aliases[0])
        extras = [alias for alias in aliases if alias and alias.lower() not in rewritten.lower()]
        if extras:
            rewritten = f"{rewritten} {' '.join(extras)}".strip()
            notes.append(f"实体扩展：{canonical or aliases[0]}")
    return {
        "rewritten": rewritten,
        "applied": rewritten != original,
        "notes": notes,
        "matched_entities": matched_entities,
    }


def infer_answer_strategy(query: str, profile: str | None = None) -> dict[str, Any]:
    """把问题粗分成知识库 / 通用 / 实时 / 工具四类。"""
    raw = str(query or "").strip()
    text = raw.lower()
    resolved_profile = profile or infer_query_profile(raw)
    if not text:
        return {"intent": "knowledge", "answer_strategy": "knowledge_rag", "reason": "空问题默认走知识库"}

    if any(token in text for token in ("天气", "气温", "下雨", "降雨", "发邮件", "发送邮件", "邮件通知", "现在几点", "今天周几", "当前时间")):
        return {"intent": "tool", "answer_strategy": "tool_first", "reason": "命中工具型问题"}
    if any(token in text for token in REALTIME_PATTERNS):
        return {"intent": "realtime", "answer_strategy": "realtime_fallback", "reason": "命中实时信息关键词"}
    if resolved_profile in {"identifier_lookup", "process_policy"} or any(token in raw for token in KNOWLEDGE_PATTERNS):
        return {"intent": "knowledge", "answer_strategy": "knowledge_rag", "reason": "企业知识问题优先走知识库"}
    if resolved_profile == "faq_semantic" or any(token in raw for token in GENERAL_PATTERNS):
        return {"intent": "general", "answer_strategy": "general_fallback", "reason": "通用解释型问题允许常识直答"}
    return {"intent": "knowledge", "answer_strategy": "knowledge_rag", "reason": "默认走知识库问答"}


def rewrite_query(
    query: str,
    config_data: dict[str, Any] | None = None,
    *,
    profile: str | None = None,
    attempt: int = 1,
    mode: str = "normal",
) -> dict[str, Any]:
    """做轻量 query rewrite，优先提升企业场景召回稳定性。"""
    cfg = _normalize_retrieval_cfg(config_data)
    rewrite_cfg = cfg["orchestration"]["rewrite"]
    original = str(query or "").strip()
    if not original or not rewrite_cfg.get("enabled", True):
        return {
            "original": original,
            "rewritten": original,
            "applied": False,
            "notes": [],
            "profile": profile or infer_query_profile(original),
        }

    rewritten = original
    resolved_profile = profile or infer_query_profile(original)
    notes: list[str] = []
    matched_entities: list[str] = []

    keyword_rules = [
        (r"报销", "报销流程 报销制度 费用审批"),
        (r"入职", "入职流程 新员工入职 办理步骤"),
        (r"SOP|流程", "SOP 操作流程 标准流程"),
        (r"合同", "合同审批 合同规范 合同流程"),
        (r"制度", "制度规范 制度说明 管理制度"),
        (r"发票", "发票要求 发票报销 发票规范"),
        (r"审批", "审批流程 审批节点 审批规范"),
        (r"发邮件|邮件", "邮件通知 邮件发送 邮件模板"),
        (r"天气", "天气预报 当前天气 城市天气"),
    ]
    profile_expansions = {
        "identifier_lookup": "编号 字段 参数 精确匹配 原文关键词",
        "keyword_exact": "关键词 原文 标题 命名",
        "faq_semantic": "定义 解释 说明 背景 总结",
        "process_policy": "流程 制度 规范 步骤 审批 节点",
    }
    if rewrite_cfg.get("expand_synonyms", True):
        for pattern, expansion in keyword_rules:
            if re.search(pattern, rewritten, flags=re.IGNORECASE):
                expanded = f"{rewritten} {expansion}".strip()
                if expanded != rewritten:
                    rewritten = expanded
                    notes.append(f"命中规则：{pattern}")

        entity_expansion = expand_entity_aliases(rewritten)
        rewritten = str(entity_expansion.get("rewritten") or rewritten).strip() or rewritten
        matched_entities = list(entity_expansion.get("matched_entities") or [])
        notes.extend(list(entity_expansion.get("notes") or []))

    if rewrite_cfg.get("attempt_expansions", True):
        expansion = profile_expansions.get(resolved_profile, "")
        if expansion and expansion not in rewritten:
            if mode == "strict":
                rewritten = f"{rewritten} {expansion}".strip()
                notes.append(f"按画像补充：{resolved_profile}")
            elif mode == "broad" and attempt >= 2:
                rewritten = f"{rewritten} {expansion}".strip()
                notes.append(f"按画像扩召回：{resolved_profile}")

    return {
        "original": original,
        "rewritten": rewritten,
        "applied": rewritten != original,
        "notes": notes,
        "profile": resolved_profile,
        "matched_entities": matched_entities,
    }


def judge_retrieval_quality(results: list[dict], config_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """评估召回质量，用于决定是否自动重试。"""
    cfg = _normalize_retrieval_cfg(config_data)
    judge_cfg = cfg["orchestration"]["judge"]
    if not results:
        return {
            "ok": False,
            "reason": "no_results",
            "top_score": 0.0,
            "avg_score": 0.0,
            "result_count": 0,
        }

    top_score = float(results[0].get("score") or 0.0)
    avg_score = sum(float(item.get("score") or 0.0) for item in results) / max(len(results), 1)
    result_count = len(results)
    ok = (
        result_count >= int(judge_cfg.get("min_results", 2))
        and top_score >= float(judge_cfg.get("min_top_score", 0.24))
        and avg_score >= float(judge_cfg.get("min_avg_score", 0.16))
    )
    reason = "ok" if ok else "low_confidence"
    if top_score >= 0.72 and avg_score >= 0.42 and result_count >= 3:
        confidence_band = "high"
    elif top_score >= float(judge_cfg.get("min_top_score", 0.24)) and avg_score >= float(judge_cfg.get("min_avg_score", 0.16)):
        confidence_band = "medium"
    else:
        confidence_band = "low"
    return {
        "ok": ok,
        "reason": reason,
        "top_score": round(top_score, 4),
        "avg_score": round(avg_score, 4),
        "result_count": result_count,
        "confidence_band": confidence_band,
    }


def get_retry_plan(config_data: dict[str, Any] | None = None) -> dict[str, Any]:
    """返回检索重试策略。"""
    cfg = _normalize_retrieval_cfg(config_data)
    retry_cfg = cfg["orchestration"]["retry"]
    return {
        "enabled": bool(retry_cfg.get("enabled", True)),
        "max_attempts": max(1, int(retry_cfg.get("max_attempts", 2) or 2)),
        "fallback_top_k": max(5, int(retry_cfg.get("fallback_top_k", 8) or 8)),
        "stages": list(retry_cfg.get("stages") or []),
    }


def choose_retrieval_route(
    query: str,
    config_data: dict[str, Any] | None = None,
    *,
    preferred_backend: str = "hybrid",
) -> dict[str, Any]:
    """根据问题画像选择默认检索路径。"""
    cfg = _normalize_retrieval_cfg(config_data)
    routing_cfg = cfg["orchestration"]["routing"]
    profile = infer_query_profile(query)
    strategy = infer_answer_strategy(query, profile)
    backend = preferred_backend
    if routing_cfg.get("enabled", True):
        backend = str((routing_cfg.get("profile_backends") or {}).get(profile) or preferred_backend)
    return {
        "profile": profile,
        "intent": strategy.get("intent", "knowledge"),
        "answer_strategy": strategy.get("answer_strategy", "knowledge_rag"),
        "backend": backend,
        "preferred_backend": preferred_backend,
        "strategy": f"profile:{profile}->{backend}",
        "explain": f"问题画像为 {profile}，优先走 {backend} 检索路径；回答策略：{strategy.get('answer_strategy', 'knowledge_rag')}",
        "reason": strategy.get("reason", ""),
    }


def build_retry_stages(
    query: str,
    config_data: dict[str, Any] | None = None,
    *,
    preferred_backend: str = "hybrid",
) -> list[dict[str, Any]]:
    """构造分级重试计划。"""
    retry_plan = get_retry_plan(config_data)
    profile = infer_query_profile(query)
    stages = list(retry_plan.get("stages") or [])
    built: list[dict[str, Any]] = []
    for index, stage in enumerate(stages, start=2):
        built.append(
            {
                "attempt": index,
                "profile": profile,
                "backend": str(stage.get("backend") or preferred_backend),
                "top_k": max(5, int(stage.get("top_k") or retry_plan.get("fallback_top_k", 8))),
                "rewrite_mode": str(stage.get("rewrite_mode") or "normal"),
                "strategy": f"retry:{index}:{profile}->{stage.get('backend') or preferred_backend}",
                "explain": f"第 {index} 次检索切换到 {stage.get('backend') or preferred_backend}，改写模式 {stage.get('rewrite_mode') or 'normal'}",
            }
        )
    return built
