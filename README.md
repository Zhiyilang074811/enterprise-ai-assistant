# Enterprise Multi-Tenant AI Assistant Platform

> **面向服务商、SaaS 运营方、集团型企业**的一站式多租户智能问答与流程助手平台
> 基于 RAG 检索增强生成 + LangChain 智能体架构，支撑多客户托管、统一交付和可持续运营的企业级 AI 解决方案

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-green.svg)](https://fastapi.tiangolo.com/)
[![LangChain](https://img.shields.io/badge/LangChain-0.3-orange.svg)](https://langchain.com/)
[![HarmonyOS](https://img.shields.io/badge/HarmonyOS-NET-red.svg)](https://developer.harmonyos.com/)
[![RAG](https://img.shields.io/badge/RAG-Enhanced-purple.svg)]()

---

## Table of Contents

- [Features](#-features)
- [Architecture](#️-architecture)
- [Tech Stack](#-tech-stack)
- [Quick Start](#-quick-start)
- [Documentation](#-documentation)
- [Use Cases](#-use-cases)
- [Contributing](#-contributing)
- [License](#-license)

---

## Features

### Multi-Tenant Architecture

| Feature | Description |
|---------|-------------|
| **Tenant Isolation** | 完整的数据和配置隔离，每个租户独立运行，支持无限扩展 |
| **Role-Based Access** | 平台管理员 / 租户管理员 / 终端用户三级角色体系 |
| **Custom Branding** | 按租户自定义 UI 品牌、Logo、主题色，支持白标输出 |
| **Scalable Deployment** | 从单机到分布式部署灵活扩展，支持双后端模式 |
| **Tenant Config** | 每个租户独立配置 LLM 模型、API Key、检索策略、Prompt |

### AI-Powered Intelligence (RAG)

| Feature | Description |
|---------|-------------|
| **RAG Pipeline** | 检索增强生成，支持多级知识库、分层权重、自动分片 |
| **Multi-LLM** | 支持 OpenAI、Claude、阿里云通义千问、国产大模型等可插拔 LLM 后端 |
| **Smart Retrieval** | 混合搜索：TF-IDF + BM25 + Dense Embedding + 重排序 (Reranker) |
| **Vector Database** | 支持 Qdrant / Milvus 双后端向量数据库，自动集合管理 |
| **Context Memory** | 会话上下文记忆，支持多轮对话 |
| **Agent Framework** | 基于 LangGraph 的智能体编排，可配置业务助手 |
| **Workflow Engine** | 可视化工作流引擎，支持 MCP 工具调用 |

### Enterprise Security

| Feature | Description |
|---------|-------------|
| **Guardrails** | 输入/输出双重内容安全护栏，保障 AI 输出合规 |
| **Audit Logging** | 完整的租户活动审计追踪（请求日志、护栏事件） |
| **Rate Limiting** | 并发控制 + 速率限制，防止 API 滥用 |
| **Balance System** | 基于话费的用量管理和余额控制 |
| **Data Encryption** | 支持 HTTPS / SSL 传输加密 |

### Analytics & Operations

| Feature | Description |
|---------|-------------|
| **Platform Dashboard** | 跨租户实时数据分析面板 |
| **Tenant Analytics** | 按租户维度的使用指标、热门问题、活跃用户 |
| **Knowledge Base** | 文档摄入、处理、自动爬取与智能检索 |
| **Evaluation** | 检索效果评估工具与跑分系统 |

### Cross-Platform

| Feature | Description |
|---------|-------------|
| **Web Admin Console** | 全功能浏览器管理后台（Admin V2） |
| **Tenant Portal** | 租户自助管理界面（Tenant V2） |
| **HarmonyOS App** | 鸿蒙原生移动应用（ArkTS + ArkUI） |
| **Voice Interaction** | 集成华为语音识别（ASR）与语音合成（TTS） |

---

## Architecture

`
+------------------------------------------------------------------+
|                       Client Layer                                |
|  +------------+  +------------+  +------------+  +------------+   |
|  |  H5 Portal |  | Admin V2   |  | Tenant V2  |  | HarmonyOS  |   |
|  | (Index)    |  | (Platform) |  | (Self-svc) |  |    App     |   |
|  +------------+  +------------+  +------------+  +------------+   |
+-------------------------------------------+----------------------+
                                            |
                                    +-------v-------+
                                    |  Nginx / GW  |
                                    +-------+-------+
                                            |
+-------------------------------------------+----------------------+
|            Backend Services (Double-Backend Mode)                 |
|                                                                   |
|  +---------------------------+  +-------------------------------+|
|  |   Business API Server     |  |         AI Engine             ||
|  |                           |  |                               ||
|  |  - Tenant Mgmt            |  |  - RAG Pipeline               ||
|  |  - Auth / RBAC            |  |    +-- Embeddings             ||
|  |  - Agent Mgmt             |  |    +-- Retriever (Hybrid)     ||
|  |  - Workflow Engine        |  |    +-- Reranker               ||
|  |  - Scheduler              |  |    +-- LLM Service            ||
|  |  - Analytics API          |  |    +-- Guardrails (2-tier)    ||
|  |  - Crawler / Ingest       |  |    +-- Memory / Context       ||
|  |  - Balance / Rate Limit   |  |    +-- Knowledge Assets       ||
|  |  - Evaluation             |  |    +-- LangGraph Agents       ||
|  +-----------+---------------+  +------------+------------------+|
|              |                                 |                 |
|  +-----------+---------------------------------+---------------+ |
|  |                      Data Layer                             | |
|  |  +------------+  +----------+  +----------+  +------------+ | |
|  |  |PostgreSQL  |  |  Redis   |  | Qdrant / |  | File /     | | |
|  |  |  (app.db)  |  |  (Cache) |  |  Milvus  |  | Markdown   | | |
|  |  +------------+  +----------+  +----------+  +------------+ | |
|  +-----------------------------------------------------------+ |
+------------------------------------------------------------------+
`

---

## Tech Stack

### Backend

| Component | Technology | Version | Purpose |
|-----------|-----------|---------|---------|
| Language | Python | 3.10+ | Core implementation |
| Framework | FastAPI | 0.115 | RESTful API server |
| Server | Uvicorn | 0.30 | ASGI server |
| AI Orchestration | LangChain + LangGraph | 0.3.x / 0.4 | LLM workflow & agent orchestration |
| Embeddings | Sentence Transformers / OpenAI | - | Text vectorization |
| RAG Engine | Custom Pipeline | - | Retrieval-Augmented Generation |
| Retriever | BM25 + TF-IDF + Dense | - | Hybrid retrieval stack |
| Reranker | Custom Service | - | Result re-ranking |
| Caching | Redis | 5.2 | Session & semantic cache |
| Security | JWT + RBAC | - | Auth & authorization |

### Vector Databases (Pluggable)

| Database | Purpose | Status |
|----------|---------|--------|
| Qdrant | Primary vector store | Supported |
| Milvus | Scalable vector store | Supported |

### Frontend

| Component | Tech | Purpose |
|-----------|------|---------|
| Login Portal | HTML5 / JS | User login & tenant selection |
| Admin Console | HTML5 / JS | Platform admin dashboard |
| Tenant Portal | HTML5 / JS | Tenant self-service management |
| Analytics | HTML5 / JS | Real-time metrics dashboards |

### HarmonyOS Mobile App

| Component | Tech | Purpose |
|-----------|------|---------|
| Language | ArkTS | Native development |
| UI Framework | ArkUI | Declarative UI |
| Backend | HTTP | REST API calls to FastAPI |
| Voice | Huawei ASR/TTS | Speech recognition & synthesis |

---

## Quick Start

### Prerequisites

- Python 3.10 or higher
- (Optional) Redis 7+ for caching
- (Optional) Qdrant or Milvus for vector storage
- LLM API Key (e.g., DashScope / OpenAI)

### Installation

`ash
# Navigate to the Python project directory
cd docs/py

# Create virtual environment
python -m venv venv

# Activate it
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure API keys
# Option 1: Edit config/api_keys.txt
# Option 2: Set environment variable
#   export DASHSCOPE_API_KEYS="sk-your-key-here"

# Start the application
bash start.sh
# Or on Windows:
python -m uvicorn backend.main:app --host 0.0.0.0 --port 6090
`

### Access the Application

| Service | URL |
|---------|-----|
| H5 Portal | http://localhost:6090 |
| Admin Console | http://localhost:6090/admin |
| Tenant Portal | http://localhost:6090/tenant |

### Default Credentials

| Role | Username | Password |
|------|----------|----------|
| Platform Admin | \platform_admin\ | \Platform@2026\ |
| Tenant Admin | \	enant_admin\ | \Tenant@2026\ |

> **Security**: Change default passwords after first login.

---

## Documentation

Complete documentation is available in the \docs/\ directory:

| Document | Description |
|----------|-------------|
| [版本与交付说明](docs/00_版本与交付说明.md) | Release notes and delivery checklist |
| [功能介绍](docs/01_功能介绍.md) | Complete feature catalog |
| [部署与启动](docs/02_部署与启动.md) | Installation and deployment guide |
| [使用教程](docs/03_使用教程.md) | Step-by-step usage guide |
| [二开与接口说明](docs/04_二开与接口说明.md) | API reference and extension guide |
| [运维排障](docs/05_运维排障.md) | Monitoring and troubleshooting |
| [检索与向量库说明](docs/06_检索与向量库说明.md) | RAG and vector database details |

### HarmonyOS App

See [harmony_app/README.md](harmony_app/README.md) for mobile app development and deployment guide.

---

## Use Cases

- **Enterprise Internal Knowledge Base** — 集中企业知识，AI 驱动的智能搜索与问答
- **Customer Service Automation** — 智能客服问答与工单处理
- **Business Process Assistant** — 复杂业务流程的 AI 引导与自动化
- **Training & Onboarding** — AI 驱动的学习助手与培训
- **Regulatory Compliance** — 智能文档审查与合规检查
- **SaaS Product Platform** — 面向客户的白标 AI 助手平台
- **Multi-Device Experience** — 支持 Web + HarmonyOS 跨设备协同

---

## Contributing

We welcome contributions! Please see our [Contributing Guide](CONTRIBUTING.md) for details.

1. Fork the repository
2. Create your feature branch (\git checkout -b feature/amazing-feature\)
3. Commit your changes (\git commit -m 'Add amazing feature'\)
4. Push to the branch (\git push origin feature/amazing-feature\)
5. Open a Pull Request

---

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

**Built for enterprises. Powered by AI. Open Source.**

Made with ❤️ by [Zhiyilang074811](https://github.com/Zhiyilang074811)
