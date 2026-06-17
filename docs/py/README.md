# Enterprise AI Assistant Platform — Python Backend

FastAPI-based multi-tenant AI assistant with RAG pipeline, LangChain/LangGraph agents, and HarmonyOS app support.

## Directory Structure

`
docs/py/
├── backend/          # FastAPI application (main.py, RAG, agents, etc.)
├── frontend/         # HTML5 admin/tenant/analytics portals
├── data/             # Configuration, knowledge, vector DB templates
├── knowledge/        # Markdown-based knowledge base
├── scripts/          # Utility scripts
├── config/           # API keys and security configs
├── requirements.txt  # Python dependencies
├── start.sh          # One-command launcher
└── generate_keys.py  # Security key generator
`

## Quick Start

`ash
cd docs/py
pip install -r requirements.txt
python -m uvicorn backend.main:app --host 0.0.0.0 --port 6090
`

## Key Modules

| Module | File | Purpose |
|--------|------|---------|
| Main App | ackend/main.py | FastAPI application with 40+ endpoints |
| RAG Engine | ackend/rag.py | Retrieval-Augmented Generation pipeline |
| Tenant Config | ackend/tenant_config.py | Multi-tenant isolation and config |
| LLM Service | ackend/llm_service.py | Multi-LLM pluggable backend |
| Guardrails | ackend/guardrails.py | Input/output content safety |
| Workflow | ackend/workflow_runtime.py | LangGraph agent orchestration |
| Retrieval | ackend/retrievers.py | BM25 + TF-IDF + Dense hybrid search |
| Knowledge | ackend/knowledge_assets.py | Knowledge base management |
| Database | ackend/database.py | SQLite/PostgreSQL ORM layer |
| Security | ackend/security_config.py | JWT auth and RBAC |
