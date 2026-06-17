"""Application configuration."""
import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
APP_CONFIG_PATH = BASE_DIR / "data" / "app_config.json"


def _clean_key_text(text: str) -> str:
    """Normalize key text and drop placeholder/example values."""
    if not text:
        return ""
    parts: list[str] = []
    for raw_part in text.replace("\n", ",").split(","):
        part = raw_part.strip()
        if not part or part.startswith("#"):
            continue
        if "your-key" in part or "your-single-key" in part or part == "sk-placeholder":
            continue
        parts.append(part)
    return ",".join(parts)


def _load_key_file() -> str:
    """Load API keys from a local config file when env vars are absent.

    Supported separators:
    - comma: sk-a,sk-b
    - newline:
      sk-a
      sk-b
    """
    key_file = BASE_DIR / "config" / "api_keys.txt"
    if not key_file.exists():
        return ""
    text = key_file.read_text(encoding="utf-8").strip()
    if not text:
        return ""
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    normalized = ",".join(lines)
    return _clean_key_text(normalized)


def _load_env_keys() -> str:
    env_text = os.getenv("DASHSCOPE_API_KEYS") or os.getenv("DASHSCOPE_API_KEY") or ""
    return _clean_key_text(env_text)

# LLM Configuration - Multi-Key Round Robin
# Support comma-separated keys: "sk-aaa,sk-bbb,sk-ccc"
_raw_keys = (
    _load_key_file()
    or _load_env_keys()
    or "sk-placeholder"
)
LLM_API_KEYS: list[str] = [k.strip() for k in _raw_keys.split(",") if k.strip()]
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
LLM_MODEL_PRIMARY = os.getenv("LLM_MODEL", "qwen-max")
LLM_MODEL_FALLBACK = os.getenv("LLM_MODEL_FALLBACK", "qwen-plus")

# Balance
INITIAL_BALANCE = int(os.getenv("INITIAL_BALANCE", "500"))  # points granted on first bind

# Database
DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "app.db")

# Knowledge base
KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "..", "knowledge")
INGEST_DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "ingest")
PLATFORM_CRAWLER_CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "crawler_config.json")
INGEST_OUTPUT_PREFIX = os.getenv("INGEST_OUTPUT_PREFIX", "auto__")
INGEST_FETCH_TIMEOUT = int(os.getenv("INGEST_FETCH_TIMEOUT", "20"))
INGEST_MAX_ITEMS_PER_SOURCE = int(os.getenv("INGEST_MAX_ITEMS_PER_SOURCE", "12"))
INGEST_USER_AGENT = os.getenv(
    "INGEST_USER_AGENT",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
)

# 默认知识层级配置。
# 运行时实际展示与目录会由 app_config 覆盖，这里只保留兜底值。
KNOWLEDGE_TIERS = {
    "hotfix":    {"label": "知识文件", "weight": 1.0, "desc": "兼容旧数据的知识目录别名"},
    "seasonal":  {"label": "知识文件", "weight": 1.0, "desc": "兼容旧数据的知识目录别名"},
    "permanent": {"label": "知识文件", "weight": 1.0, "desc": "兼容旧数据的知识目录别名"},
}

# RAG settings
RAG_TOP_K = 5
RAG_CHUNK_SIZE = 500
RAG_CHUNK_OVERLAP = 50

# Semantic cache
CACHE_TTL_SECONDS = 600  # 10 minutes
CACHE_SIMILARITY_THRESHOLD = 0.85

# Rate limiting - frontend anti-abuse guard, not a full concurrency control strategy
RATE_LIMIT_MAX_REQUESTS = 1
RATE_LIMIT_WINDOW_SECONDS = 10

# In-process concurrency guards for chat traffic
GLOBAL_CHAT_CONCURRENCY_LIMIT = int(os.getenv("GLOBAL_CHAT_CONCURRENCY_LIMIT", "40"))
TENANT_CHAT_CONCURRENCY_LIMIT = int(os.getenv("TENANT_CHAT_CONCURRENCY_LIMIT", "12"))
AGENT_CHAT_CONCURRENCY_LIMIT = int(os.getenv("AGENT_CHAT_CONCURRENCY_LIMIT", "6"))
GLOBAL_LLM_CONCURRENCY_LIMIT = int(os.getenv("GLOBAL_LLM_CONCURRENCY_LIMIT", "24"))
GLOBAL_WORKFLOW_IO_CONCURRENCY_LIMIT = int(os.getenv("GLOBAL_WORKFLOW_IO_CONCURRENCY_LIMIT", "20"))

# Admin credentials
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "platform_admin").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "Platform@2026").strip()

# Server
HOST = "0.0.0.0"
PORT = int(os.getenv("PORT", "6090"))


def load_ingest_extra_sources() -> list[dict]:
    """Compatibility shim for legacy code paths; platform crawler sources are now stored in data/crawler_config.json."""
    return []
