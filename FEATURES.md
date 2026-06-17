# Complete Feature List

## 1. Multi-Tenant Architecture

- Per-tenant data isolation
- Per-tenant LLM model configuration
- Per-tenant API Key pool
- Per-tenant retrieval strategy
- Per-tenant system prompt
- Tenant theme and branding customization
- Phone-based account system with device binding
- Balance management and consumption tracking
- Rate limiting per tenant

## 2. RAG (Retrieval-Augmented Generation)

- Hybrid retrieval: TF-IDF + BM25 + Dense
- Hierarchical knowledge base (hotfix / seasonal / permanent)
- Automatic document chunking and embedding
- Result reranking
- Knowledge source display in responses
- Knowledge metadata tracking
- Support for multiple document formats (PDF, Word, PowerPoint, Markdown)

## 3. AI Agent Framework

- LangGraph-based agent orchestration
- Multiple configurable agents per tenant
- User-agent binding
- Agent publish API keys
- Agent usage analytics
- Pluggable LLM backend (DashScope, OpenAI, Claude, domestic models)

## 4. Workflow Engine

- Configurable business process workflows
- MCP (Model Context Protocol) tool integration
- Business tool snapshots
- Workflow execution tracking

## 5. Security

- Input guardrails (content filtering)
- Output guardrails (safety checks)
- Request audit logging
- Guardrail event logging
- Concurrency control (chat, LLM, workflow)
- Rate limiting
- JWT authentication
- RBAC (Role-Based Access Control)
- SSL/TLS support
- Balance-based access control

## 6. Analytics

### Platform Level
- Cross-tenant usage overview
- Total active users
- Total requests
- Platform health metrics

### Tenant Level
- Usage summary
- Daily trends
- Agent usage statistics
- Top questions
- Active user counts
- Hourly distribution
- Chat annotation summaries
- Label distribution

## 7. Knowledge Management

- Knowledge library management
- Knowledge category support
- Knowledge tag system
- Knowledge tag groups
- File metadata management
- Knowledge asset tracking
- Generic web crawler for knowledge ingestion
- Crawler run history and evaluation

## 8. Evaluation

- Retrieval quality evaluation framework
- Evaluation run tracking
- Performance metrics

## 9. Mobile (HarmonyOS)

- Native ArkTS + ArkUI app
- Phone number login
- Stream chat (SSE)
- Agent selection and switching
- Knowledge Q&A
- Context memory
- Voice interaction (ASR/TTS)
- Cross-device sync
