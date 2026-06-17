"""检索后端配置管理。"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from backend.tenant_config import ensure_tenant_storage, get_tenant_retrieval_config_path

BASE_DIR = Path(__file__).resolve().parent.parent
RETRIEVAL_CONFIG_PATH = BASE_DIR / "data" / "retrieval_config.json"

DEFAULT_RETRIEVAL_CONFIG = {
    "backend": "hybrid",
    "dense_provider": "qdrant",
    "qdrant": {
        "enabled": True,
        "mode": "local",
        "url": "http://127.0.0.1:6333",
        "api_key": "",
        "path": "data/qdrant_store",
        "collection": "enterprise_rag_default",
        "vector_size": 1024,
        "distance": "Cosine",
    },
    "milvus": {
        "enabled": False,
        "uri": "http://127.0.0.1:19530",
        "token": "",
        "user": "",
        "password": "",
        "db_name": "default",
        "collection": "enterprise_rag_default",
        "vector_size": 1024,
        "metric_type": "COSINE",
        "consistency_level": "Bounded",
    },
    "embedding": {
        "provider": "local_hash",
        "model": "local_hash_v1",
        "base_url": "",
        "api_key": "",
    },
    "rerank": {
        "enabled": True,
        "provider": "local_overlap",
        "model": "local_overlap_v1",
        "base_url": "",
        "api_key": "",
        "candidate_limit": 12,
        "top_n": 5,
    },
    "sparse": {
        "enabled": True,
        "provider": "bm25",
        "k1": 1.5,
        "b": 0.75,
        "dense_weight": 0.6,
        "sparse_weight": 0.4,
        "fusion_alpha": 0.7,
        "rrf_k": 50,
        "query_profiles": {
            "keyword_exact": {"dense_weight": 0.35, "sparse_weight": 0.65, "fusion_alpha": 0.55},
            "identifier_lookup": {"dense_weight": 0.25, "sparse_weight": 0.75, "fusion_alpha": 0.45},
            "faq_semantic": {"dense_weight": 0.72, "sparse_weight": 0.28, "fusion_alpha": 0.82},
            "process_policy": {"dense_weight": 0.58, "sparse_weight": 0.42, "fusion_alpha": 0.7},
        },
    },
    "orchestration": {
        "rewrite": {
            "enabled": True,
            "expand_synonyms": True,
            "attempt_expansions": True,
        },
        "judge": {
            "min_results": 2,
            "min_top_score": 0.24,
            "min_avg_score": 0.16,
        },
        "routing": {
            "enabled": True,
            "profile_backends": {
                "identifier_lookup": "bm25",
                "keyword_exact": "hybrid",
                "faq_semantic": "dense",
                "process_policy": "hybrid",
            },
        },
        "retry": {
            "enabled": True,
            "max_attempts": 2,
            "fallback_top_k": 8,
            "stages": [
                {"backend": "hybrid", "top_k": 8, "rewrite_mode": "broad"},
                {"backend": "bm25", "top_k": 10, "rewrite_mode": "strict"},
            ],
        },
    },
}


def normalize_retrieval_backend_name(value: str | None) -> str:
    clean = str(value or "").strip().lower()
    if clean in {"qdrant", "milvus", "dense", "vector", "vector_store"}:
        return "dense"
    if clean in {"hybrid", "bm25", "local_tfidf"}:
        return clean
    return "hybrid"


def normalize_dense_provider_name(value: str | None) -> str:
    clean = str(value or "").strip().lower()
    if clean in {"milvus"}:
        return "milvus"
    return "qdrant"


def _resolve_retrieval_config_path(tenant_id: str | None = None, tenant_name: str = "") -> Path:
    """解析检索配置路径。"""
    if tenant_id:
        ensure_tenant_storage(tenant_id, tenant_name or tenant_id)
        return get_tenant_retrieval_config_path(tenant_id)
    return RETRIEVAL_CONFIG_PATH


def resolve_qdrant_local_path(path_value: str | None) -> Path:
    """把相对路径解析到项目根目录，避免本地嵌入式 Qdrant 写到未知位置。"""
    raw = str(path_value or "").strip()
    if not raw:
        raw = str(DEFAULT_RETRIEVAL_CONFIG["qdrant"]["path"])
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _normalize_backend_fields(config: dict) -> dict:
    config["backend"] = normalize_retrieval_backend_name(config.get("backend"))
    orchestration = config.setdefault("orchestration", {})
    routing = orchestration.setdefault("routing", {})
    profile_backends = routing.get("profile_backends")
    if isinstance(profile_backends, dict):
        routing["profile_backends"] = {
            key: normalize_retrieval_backend_name(value)
            for key, value in profile_backends.items()
        }
    retry = orchestration.setdefault("retry", {})
    stages = retry.get("stages")
    if isinstance(stages, list):
        normalized_stages: list[dict] = []
        for item in stages:
            if not isinstance(item, dict):
                continue
            next_item = dict(item)
            next_item["backend"] = normalize_retrieval_backend_name(item.get("backend"))
            normalized_stages.append(next_item)
        retry["stages"] = normalized_stages
    return config


def _normalize_dense_provider(config: dict) -> dict:
    qdrant_cfg = config.setdefault("qdrant", {})
    milvus_cfg = config.setdefault("milvus", {})
    config["dense_provider"] = normalize_dense_provider_name(
        config.get("dense_provider")
        or ("milvus" if str(config.get("vector_store_provider") or "").strip().lower() == "milvus" else "")
    )
    if "uri" not in milvus_cfg and milvus_cfg.get("url"):
        milvus_cfg["uri"] = milvus_cfg.get("url")
    if "metric_type" not in milvus_cfg and milvus_cfg.get("distance"):
        milvus_cfg["metric_type"] = str(milvus_cfg.get("distance") or "").upper()
    if not milvus_cfg.get("vector_size"):
        milvus_cfg["vector_size"] = qdrant_cfg.get("vector_size") or DEFAULT_RETRIEVAL_CONFIG["milvus"]["vector_size"]
    if not milvus_cfg.get("collection"):
        milvus_cfg["collection"] = qdrant_cfg.get("collection") or DEFAULT_RETRIEVAL_CONFIG["milvus"]["collection"]
    return config


def normalize_retrieval_config(config_data: dict | None) -> dict:
    base = _deep_merge(DEFAULT_RETRIEVAL_CONFIG, config_data if isinstance(config_data, dict) else {})
    base = _normalize_backend_fields(base)
    base = _normalize_dense_provider(base)
    return base


def resolve_dense_provider(config_data: dict | None = None) -> str:
    config = normalize_retrieval_config(config_data)
    return normalize_dense_provider_name(config.get("dense_provider"))


def get_dense_store_config(config_data: dict | None = None, provider: str | None = None) -> dict:
    config = normalize_retrieval_config(config_data)
    dense_provider = normalize_dense_provider_name(provider or config.get("dense_provider"))
    return dict(config.get(dense_provider) or {})


def resolve_dense_vector_size(config_data: dict | None = None) -> int:
    dense_cfg = get_dense_store_config(config_data)
    return int(dense_cfg.get("vector_size") or 1024)


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_retrieval_config_file(tenant_id: str | None = None, tenant_name: str = "") -> None:
    """首次启动时补齐检索后端配置。"""
    config_path = _resolve_retrieval_config_path(tenant_id, tenant_name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if not config_path.exists():
        config_path.write_text(
            json.dumps(DEFAULT_RETRIEVAL_CONFIG, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_retrieval_config(tenant_id: str | None = None, tenant_name: str = "") -> dict:
    """读取检索后端配置。"""
    ensure_retrieval_config_file(tenant_id, tenant_name)
    config_path = _resolve_retrieval_config_path(tenant_id, tenant_name)
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        raw = {}
    return normalize_retrieval_config(raw if isinstance(raw, dict) else {})


def save_retrieval_config(config_data: dict, tenant_id: str | None = None, tenant_name: str = "") -> dict:
    """保存检索后端配置。"""
    if not isinstance(config_data, dict):
        raise ValueError("检索配置必须是 JSON 对象")
    merged = normalize_retrieval_config(config_data)
    config_path = _resolve_retrieval_config_path(tenant_id, tenant_name)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(merged, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return merged
