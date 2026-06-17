# 企业级多租户智能问答流程助手平台

> **面向服务商、SaaS 运营方、集团型企业**的一站式多租户智能问答与流程助手平台
> 基于 RAG 检索增强生成 + LangChain/LangGraph 智能体架构，支撑多客户托管、统一交付和可持续运营的企业级 AI 解决方案

[English](README.md) | **中文版**

---

## 项目简介

本项目是一套面向**服务商、SaaS 运营方、集团型企业**设计的企业级智能问答与流程助手平台。核心定位是支撑**多客户托管、统一交付和可持续运营**的企业级智能问答流程助手。

### 核心能力

- **多租户 SaaS 架构** — 完整的租户隔离，每个租户独立配置 LLM、API Key、检索策略、Prompt
- **RAG 检索增强生成** — 混合检索（TF-IDF + BM25 + Dense）+ 智能重排序
- **LangGraph 智能体编排** — 可配置的业务助手和工作流引擎
- **企业级安全** — 双重 Guardrails、审计日志、并发控制、余额管理
- **跨平台** — Web 管理后台 + 租户门户 + 鸿蒙原生 App
- **双后端部署** — 支持单机和分布式部署模式

## 技术栈

| 层级 | 技术 | 说明 |
|------|------|------|
| 后端 | Python 3.10+ / FastAPI 0.115 | RESTful API 服务 |
| AI | LangChain 0.3 + LangGraph 0.4 | LLM 工作流与智能体编排 |
| 检索 | BM25 + TF-IDF + Dense + Reranker | 混合检索 + 重排序 |
| 向量库 | Qdrant / Milvus | 可插拔向量数据库 |
| 缓存 | Redis 5.2 | 会话与语义缓存 |
| 前端 | HTML5 / JavaScript | 管理后台 + 租户门户 |
| 鸿蒙 | ArkTS + ArkUI | 鸿蒙原生移动应用 |

## 快速开始

`ash
cd docs/py
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
bash start.sh
`

访问 http://localhost:6090

## 完整文档

详见 [README.md](README.md)（英文版）和 docs/ 目录下的中文文档。

## 鸿蒙 App

详见 [harmony_app/README.md](harmony_app/README.md)

## 许可证

MIT License — 详见 [LICENSE](LICENSE) 文件

---

**Built for enterprises. Powered by AI. Open Source.**

Made with by [Zhiyilang074811](https://github.com/Zhiyilang074811)
