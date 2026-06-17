# Deployment Guide

## Overview

This platform supports multiple deployment modes:

1. **Single Server** — All services on one machine (development / small deployments)
2. **Double Backend** — Separated business API and AI engine (production)
3. **Distributed** — Scaled across multiple servers

## Prerequisites

- Python 3.10+
- Redis 7+ (optional but recommended)
- PostgreSQL 14+ (for production)
- Qdrant or Milvus (for vector storage)

## Deployment Steps

### Step 1: Install Dependencies

`ash
cd docs/py
pip install -r requirements.txt
`

### Step 2: Configure API Keys

Create config/api_keys.txt:
`
sk-your-dashscope-key-here
sk-your-openai-key-here
`

Or set environment variable:
`ash
export DASHSCOPE_API_KEYS="sk-your-key-here"
`

### Step 3: Configure Vector Database

For **Qdrant** (default):
`ash
docker run -p 6333:6333 qdrant/qdrant
`

For **Milvus**:
`ash
docker compose -f docker-compose-milvus.yml up -d
`

### Step 4: Start the Application

`ash
bash start.sh
`

Or manually:
`ash
python -m uvicorn backend.main:app --host 0.0.0.0 --port 6090
`

## Configuration Files

| File | Purpose |
|------|---------|
| data/app_config.json | App branding and theme |
| data/model_config.json | LLM model selection |
| data/retrieval_config.json | RAG retrieval settings |
| data/security_config.json | Security settings |
| data/tool_config.json | MCP tool configuration |
| data/workflow_config.json | Workflow settings |
| config/api_keys.txt | LLM API keys |

## Production Checklist

- [ ] Change default admin passwords
- [ ] Enable HTTPS / SSL
- [ ] Use PostgreSQL instead of SQLite
- [ ] Configure Redis for caching
- [ ] Set up monitoring and alerting
- [ ] Configure backup strategy
- [ ] Review security_config.json
- [ ] Set VERIFY_SSL=1 for strict SSL verification
