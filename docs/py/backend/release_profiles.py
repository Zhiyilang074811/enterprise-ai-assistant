"""销售版本配置、导出规则与交付文档生成。"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output" / "releases"
SAFE_QDRANT_PLACEHOLDER = {
    "enabled": True,
    "mode": "local",
    "url": "",
    "api_key": "",
    "path": "data/qdrant_store_template",
}
SAFE_MILVUS_PLACEHOLDER = {
    "enabled": False,
    "uri": "http://127.0.0.1:19530",
    "token": "",
    "user": "",
    "password": "",
    "db_name": "default",
}
SAFE_WORKFLOW_TEMPLATE = {
    "default_workflow_id": "main",
    "items": [
        {
            "workflow_id": "main",
            "name": "默认工作流",
            "description": "当前租户主流程。",
            "enabled": True,
            "sort_order": 100,
            "version": "V1.0",
            "status": "draft",
            "updated_at": "",
            "nodes": [],
            "connections": [],
            "app_overrides": {
                "chat_title": "",
                "chat_tagline": "",
                "welcome_message": "",
                "agent_description": "",
                "recommended_questions": [],
                "input_placeholder": "",
                "send_button_text": "",
            },
            "system_prompt": "你是企业知识库的租户专属智能助理。\n\n请优先根据下方知识库内容回答；如果知识库没有明确答案，先说明知识不足，再给出谨慎建议。\n\n【知识库内容开始】\n{knowledge_context}\n【知识库内容结束】\n",
        }
    ],
}
SAFE_APP_CONFIG_TEMPLATE_FIELDS = {
    "app_name": "",
    "app_subtitle": "",
    "chat_title": "",
    "chat_tagline": "",
    "welcome_message": "",
    "agent_description": "",
    "recommended_questions": [],
    "login_hint": "",
    "input_placeholder": "",
    "send_button_text": "",
}
SERVICE_PROVIDER_SAMPLE_TENANT_ID = "huadong_hospital"
SERVICE_PROVIDER_SAMPLE_TENANT_NAME = "华东协同医院"
DEFAULT_PLATFORM_ADMIN_USERNAME = "platform_admin"
DEFAULT_PLATFORM_ADMIN_PASSWORD = "Platform@2026"
DEFAULT_TENANT_ADMIN_USERNAME = "tenant_admin"
DEFAULT_TENANT_ADMIN_PASSWORD = "Tenant@2026"

RELEASE_PROFILES = {
    "enterprise": {
        "key": "enterprise",
        "label": "企业版",
        "deployment_mode": "single_backend",
        "summary": "单后台版本，适合个人、小团队、单企业私有部署。",
        "features": {
            "platform_admin": False,
            "tenant_admin": True,
            "platform_logs": False,
            "multi_tenant_manage": False,
            "factory_center": False,
            "release_export": False,
        },
        "entries": {
            "backend": "/tenant",
            "login": "/login",
            "chat": "/chat",
        },
        "menus": {
            "tenant": [
                "账户管理",
                "企业定制化",
                "工作流配置",
                "知识库设置",
                "Python脚本设置",
                "模型配置",
                "日志查看",
                "问答测试",
            ]
        },
        "frontend_files": [
            "login_v2.html",
            "tenant_v2.html",
            "index_v2.html",
            "analytics_v2.html",
        ],
        "docs_profile": {
            "audience": "单企业私有部署客户",
            "delivery_focus": [
                "单后台交付，部署后即可进入企业控制台",
                "多智能体、工作流、知识库与模型配置集中在一个后台",
                "前台登录与聊天入口可直接交给终端用户使用",
            ],
        },
    },
    "service_provider": {
        "key": "service_provider",
        "label": "服务商版",
        "deployment_mode": "double_backend",
        "summary": "双后台版本，适合服务商、多客户托管和 SaaS 运营。",
        "features": {
            "platform_admin": True,
            "tenant_admin": True,
            "platform_logs": True,
            "multi_tenant_manage": True,
            "factory_center": False,
            "release_export": False,
        },
        "entries": {
            "platform": "/admin",
            "tenant": "/tenant",
            "login": "/login",
            "chat": "/chat",
        },
        "menus": {
            "platform": [
                "企业账户管理",
                "平台总日志",
            ],
            "tenant": [
                "账户管理",
                "企业定制化",
                "工作流配置",
                "知识库设置",
                "Python脚本设置",
                "模型配置",
                "日志查看",
                "问答测试",
            ],
        },
        "frontend_files": [
            "login_v2.html",
            "admin_v2.html",
            "tenant_v2.html",
            "index_v2.html",
            "analytics_v2.html",
            "platform_analytics_v2.html",
        ],
        "docs_profile": {
            "audience": "服务商、多客户托管方、SaaS 运营方",
            "delivery_focus": [
                "平台后台 + 租户后台双后台交付",
                "支持多企业客户统一管理与分租户配置",
                "统一运营、多智能体装配、工作流交付与客户隔离并存",
            ],
        },
    },
}


def list_release_profiles() -> list[dict]:
    return [copy.deepcopy(item) for item in RELEASE_PROFILES.values()]


def get_release_profile(profile_key: str) -> dict:
    key = str(profile_key or "").strip()
    if key not in RELEASE_PROFILES:
        raise ValueError(f"未知版本：{profile_key}")
    return copy.deepcopy(RELEASE_PROFILES[key])


def build_release_app_config(*, profile: dict, current_app_config: dict) -> dict:
    merged = copy.deepcopy(current_app_config)
    merged["edition"] = profile["key"]
    merged["deployment_mode"] = profile["deployment_mode"]
    merged["feature_flags"] = copy.deepcopy(profile.get("features") or {})
    merged["release_profile"] = {
        "key": profile["key"],
        "label": profile["label"],
        "summary": profile["summary"],
        "entries": copy.deepcopy(profile.get("entries") or {}),
        "docs_profile": profile["key"],
    }
    merged["factory_enabled"] = False
    return merged


def _format_menu_lines(profile: dict) -> str:
    parts: list[str] = []
    for role, items in (profile.get("menus") or {}).items():
        role_label = "平台后台" if role == "platform" else "租户后台"
        parts.append(f"## {role_label}")
        parts.extend(f"- {item}" for item in items)
        parts.append("")
    return "\n".join(parts).strip()


def _format_entry_lines(profile: dict) -> str:
    labels = {
        "platform": "平台后台",
        "tenant": "租户后台",
        "backend": "后台入口",
        "login": "登录入口",
        "chat": "聊天入口",
    }
    lines = []
    for key, path in (profile.get("entries") or {}).items():
        lines.append(f"- {labels.get(key, key)}：`{path}`")
    return "\n".join(lines)


def _format_feature_lines(profile: dict) -> str:
    flags = profile.get("features") or {}
    label_map = {
        "platform_admin": "平台后台",
        "tenant_admin": "租户后台",
        "platform_logs": "平台总日志",
        "multi_tenant_manage": "多租户管理",
        "factory_center": "母版打包中心",
        "release_export": "版本导出能力",
    }
    return "\n".join(
        f"- {label_map.get(key, key)}：{'开启' if value else '关闭'}"
        for key, value in flags.items()
    )


def _build_release_readme(profile: dict) -> str:
    focus = "\n".join(f"- {item}" for item in profile["docs_profile"]["delivery_focus"])
    return f"""# {profile['label']}

这是根据当前母版源码自动裁剪出来的 {profile['label']} 交付包。

## 版本定位

- 版本名称：{profile['label']}
- 部署模式：`{profile['deployment_mode']}`
- 适用对象：{profile['docs_profile']['audience']}
- 版本简介：{profile['summary']}

## 默认入口

{_format_entry_lines(profile)}

## 交付重点

{focus}

## 你先看哪些文档

- `docs/00_版本与交付说明.md`：先看版本差异、默认入口、交付边界
- `docs/01_功能介绍.md`：给客户讲产品、给交付讲能力、给技术讲结构
- `docs/02_部署与启动.md`：部署方式、本地启动、初始化顺序
- `docs/03_使用教程.md`：后台怎么用、客户怎么配置、常见操作顺序
- `docs/04_二开与接口说明.md`：代码入口、接口分组、配置文件、再次发版
- `docs/05_运维排障.md`：日志、数据库、知识库、常见故障处理
- `docs/06_检索与向量库说明.md`：Hybrid / Dense / BM25、Qdrant / Milvus 配置说明
"""


def _build_docs_index(profile: dict) -> str:
    return f"""# {profile['label']} 文档索引

- `00_版本与交付说明.md`：版本定位、适用客户、入口、交付范围
- `01_功能介绍.md`：业务价值、核心能力、技术结构、版本差异
- `02_部署与启动.md`：本地启动、生产部署、初始化与验收
- `03_使用教程.md`：平台后台、租户后台、聊天前台的操作教程
- `04_二开与接口说明.md`：代码结构、配置文件、接口分组、再次发版
- `05_运维排障.md`：日志、数据目录、备份、升级、常见问题
- `06_检索与向量库说明.md`：检索模式、向量库、Milvus 与交付建议
"""


def _build_version_doc(profile: dict) -> str:
    title = f"{profile['label']}交付说明"
    if profile["key"] == "enterprise":
        title = "企业版交付说明"
    elif profile["key"] == "service_provider":
        title = "服务商版交付说明"
    default_account_block = f"""
## 默认登录账号

- 租户后台账号：`{DEFAULT_TENANT_ADMIN_USERNAME}`
- 租户后台密码：`{DEFAULT_TENANT_ADMIN_PASSWORD}`
"""
    if profile["key"] == "service_provider":
        default_account_block = f"""
## 默认登录账号

- 平台后台账号：`{DEFAULT_PLATFORM_ADMIN_USERNAME}`
- 平台后台密码：`{DEFAULT_PLATFORM_ADMIN_PASSWORD}`
- 租户后台账号：`{DEFAULT_TENANT_ADMIN_USERNAME}`
- 租户后台密码：`{DEFAULT_TENANT_ADMIN_PASSWORD}`
"""
    return f"""# {title}

## 当前版本

- 版本名称：{profile['label']}
- 部署模式：`{profile['deployment_mode']}`
- 适用对象：{profile['docs_profile']['audience']}
- 版本简介：{profile['summary']}

## 交付入口

{_format_entry_lines(profile)}

## 功能开关

{_format_feature_lines(profile)}

## 默认后台能力

{_format_menu_lines(profile)}

## 当前交付范围

- 多智能体管理
- 工作流配置与测试
- 知识库、分类、标签、文件管理
- 模型与检索配置
- 工具与 MCP 服务接入
- 运行日志、问答测试、评测与采集调度

## 当前不包含什么

- 不携带真实模型 Key、真实向量索引和正式业务数据库
- 不默认附带生产环境的 Nginx、Supervisor、Systemd 等宿主机配置
- 不建议把母版仓直接当客户生产目录使用，正式交付请以当前导出包为准

{default_account_block}
"""


def _build_feature_intro_doc(profile: dict) -> str:
    if profile["key"] == "service_provider":
        return """# 服务商版功能介绍

## 产品定位

服务商版面向多客户托管、SaaS 运营和统一交付场景设计。

交付内容不是单一问答页面，而是一套可持续运营的企业智能体平台：

- 平台侧可以统一管理多个企业客户
- 每个企业客户都有自己独立的租户后台
- 每个租户下面又可以配置多个智能体、多个知识范围和多个工作流

## 适用场景

- 平台运营团队负责开通客户、管理客户和查看平台总览
- 每个客户的管理员进入自己的租户后台，维护品牌、模型、知识和流程
- 最终用户从登录页进入前台，直接使用已经配置好的业务助手

适合以下业务形态：

- 服务商统一管理多个企业客户
- SaaS 平台提供企业知识助手服务
- 集团总部统一服务多个下属单位
- 同一套平台持续交付多个项目客户

## 核心能力

### 1. 一套平台，支持多个客户同时交付

- 每个客户都有独立租户空间
- 每个客户的知识、工作流、日志和账号彼此隔离
- 新客户上线时，不需要再单独起一套系统

### 2. 一个客户下，可以配置多个业务助手

- 可以按部门、角色、业务场景拆分不同智能体
- 每个智能体都能独立配置名称、欢迎语、推荐问题、知识范围和工作流
- 前台用户看到的是业务入口，不需要理解后台配置细节

### 3. 知识管理适合长期运营

- 支持知识库、分类、标签、文件四层管理
- 支持按业务范围圈定知识，不会所有资料都混在一起
- 适合客户后期持续补充文档、制度、产品资料和通知内容

### 4. 不只是问答，还能做流程型助手

工作流支持：

- 知识检索
- AI 生成
- 条件判断
- 外部 HTTP 接口
- MCP 服务
- 脚本执行
- 子流程编排

适合做制度问答、客服助手、销售助手、交付助手、培训助手等场景。

## 平台包含哪些后台

### 平台后台

平台后台面向平台运营方使用，主要解决：

- 客户开通
- 多租户管理
- 平台级日志查看
- 平台级统计分析

### 租户后台

租户后台面向客户管理员使用，主要解决：

- 企业品牌与文案配置
- 智能体管理
- 知识库与文件管理
- 工作流配置与测试
- 模型、检索、工具、MCP 配置
- 日志、评测、采集查看

### 前台使用入口

前台面向最终业务用户，主要解决：

- 登录
- 选择智能体
- 发起会话
- 查看历史记录
- 连续追问

## 交付价值

- 一套可直接部署的后端与前端源码
- 已按服务商版裁剪好的页面和配置结构
- 支持继续二开，不是封闭黑盒
- 支持后续继续扩客户、扩知识、扩工作流

## 适用对象

- 服务商
- SaaS 平台
- 集团内共享一个平台服务多个下属单位的团队
- 需要统一运营多个企业 AI 项目的团队
"""
    return """# 企业版功能介绍

## 产品定位

企业版面向单企业私有化部署和单客户项目交付场景设计。

交付内容是一套可直接部署的企业智能体控制台：

- 一个后台完成品牌、知识、工作流和模型配置
- 一个前台提供最终用户登录和问答使用
- 不带平台多租户管理层，结构更直接，部署也更轻

## 适用场景

- 企业管理员进入后台，维护知识、智能体和流程
- 不同业务场景可以配置不同智能体
- 终端员工或业务人员从登录页进入前台直接使用

适合以下业务形态：

- 企业内部知识助手建设
- 单客户私有化项目交付
- 部门级制度、SOP、产品资料问答场景
- 希望快速上线并持续优化的企业智能体项目

## 核心能力

### 1. 一个企业可以同时管理多个业务助手

- 可以按制度、产品、客服、培训、交付等场景拆分智能体
- 每个智能体都可以独立配置名称、欢迎语、推荐问题、知识范围和工作流
- 后台统一管理，前台按业务入口使用

### 2. 知识库不是临时上传，而是可长期维护

- 支持知识库、分类、标签、文件四层管理
- 支持多种格式资料导入
- 支持按智能体或业务范围管理知识，不会所有资料混成一个总库

### 3. 可以把业务流程沉淀成工作流

工作流支持：

- 知识检索
- AI 生成
- 条件判断
- HTTP 接口
- MCP 服务
- 脚本节点
- 子流程

这意味着系统不仅能回答问题，还能把回答逻辑、业务规则和外部调用一起固化下来。

### 4. 后期持续优化有抓手

- 能看聊天日志
- 能看请求日志
- 能看护栏事件
- 能做问答测试和评测
- 能持续优化知识、Prompt、模型和流程

## 企业版包含哪些部分

### 企业后台

企业后台面向管理员使用，主要解决：

- 企业品牌与文案配置
- 智能体管理
- 知识库与文件管理
- 工作流配置与测试
- 模型、检索、工具、MCP 配置
- 日志、评测、采集查看

### 前台使用入口

前台面向最终业务用户，主要解决：

- 登录
- 选择智能体
- 发起会话
- 查看历史记录
- 连续追问

## 交付价值

- 一套可直接部署的企业版源码
- 已按企业版裁剪好的页面和配置结构
- 部署结构简单，适合单客户项目快速上线
- 后续仍可继续二开，不受封闭平台限制

## 适用对象

- 单企业私有化部署
- 内部知识助手项目
- 单客户交付项目
- 希望先把一个企业 AI 项目快速落地的团队
"""


def _build_deploy_doc(profile: dict) -> str:
    entry_hint = "部署完成后，管理员从 `/tenant` 进入后台，最终用户从 `/login` 进入前台。"
    title = "企业版部署与启动"
    if profile["key"] == "service_provider":
        title = "服务商版部署与启动"
        entry_hint = "部署完成后，平台管理员从 `/admin` 进入平台后台，企业客户管理员从 `/tenant` 进入租户后台，最终用户从 `/login` 进入前台。"
    return f"""# {title}

## 最低启动方式

```bash
pip install -r requirements.txt
python3 -m uvicorn backend.main:app --host 0.0.0.0 --port 6090 --reload
```

浏览器访问：

- 聊天首页：`/chat`
- 登录页：`/login`
- 后台入口：按版本自动生效

{entry_hint}

## 目录准备

- `config/`：放环境配置与密钥文件
- `data/`：放数据库、平台配置、租户配置、检索配置
- `knowledge/`：放知识文件
- `frontend/`：放静态页面

## 初始化顺序

1. 安装 Python 依赖并启动服务
2. 部署后填写模型配置和接口鉴权信息
3. 登录后台，补齐品牌配置和登录提示
4. 导入知识文件
5. 配置或导入工作流
6. 创建正式管理员和正式业务账号
7. 做问答测试和日志验证

## 服务器建议配置

- 测试或演示环境：2 核 CPU、4GB 内存、20GB 可用磁盘
- 正式起步环境：4 核 CPU、8GB 到 16GB 内存、50GB 以上可用磁盘
- 如果知识库规模更大、并发更高或工作流更复杂，建议按 8 核 CPU、16GB 以上内存评估

## 操作系统建议

- 开发测试环境：macOS、Linux、Windows 都可以
- 正式环境：建议 Linux 服务器
- 常见选择：Ubuntu 20.04 或 22.04

## 部署时需要准备什么

- 服务器和操作系统
- Python 运行环境
- 反向代理或网关配置
- 大模型、Embedding、Rerank 等实际接口信息
- 正式管理员账号和正式业务账号

## 检索与向量库准备

- 当前交付包支持 `Hybrid`、`Dense`、`BM25` 三种检索模式
- Dense 向量库支持 `Qdrant` 和 `Milvus` 两种 Provider
- 演示或本地自测时，可以直接使用本地 Qdrant 目录，或把 Milvus 指向一个本地文件路径做 Lite 模式验证
- 正式环境建议按客户实际情况选择远程 Qdrant 服务或远程 Milvus 服务，并把对应数据目录或服务地址纳入持久化和备份范围
- 如果切到 Milvus，不需要重新上传知识文件，本质上是基于现有知识目录重建向量索引

## 首次初始化账号说明

- 交付包默认带一套可直接登录的初始化账号
- 平台管理员账号可通过 `ADMIN_USERNAME`、`ADMIN_PASSWORD` 覆盖
- 默认租户管理员账号可通过 `DEFAULT_TENANT_ADMIN_USERNAME`、`DEFAULT_TENANT_ADMIN_PASSWORD` 覆盖
- 正式上线前，建议在首次登录后立即修改默认密码

## 上线前建议确认

- 对外访问域名或内网地址
- 文件上传目录和数据目录是否持久化
- 是否需要 HTTPS
- 是否需要和现有账号体系或业务系统打通

## 生产部署建议

- 进程层建议用 `systemd`、`supervisor` 或容器方式托管
- HTTP 层建议用 Nginx 反向代理
- 正式环境把数据库目录、知识目录和向量目录做持久化
- 升级前先备份 `data/`、`knowledge/`、`.env` 和反向代理配置

## 发布前验收

- 页面能正常打开：登录页、聊天页、后台页
- 模型配置能保存并正常调用
- 上传一份知识文件后能完成切片和检索
- 检索模式切到 `Hybrid / Dense` 后，Qdrant 或 Milvus 至少要完成一次实际问答验证
- 默认工作流能运行
- 日志页能看到请求日志和聊天日志
"""


def _build_tutorial_doc(profile: dict) -> str:
    title = "企业版使用教程"
    platform_block = ""
    if profile["key"] == "service_provider":
        title = "服务商版使用教程"
        platform_block = """
## 平台后台怎么用

- 先开通企业租户，生成租户后台账号
- 再给每个租户准备品牌、模型、知识和工作流
- 通过平台总日志看整体调用和异常情况
- 需要交付多个客户时，用平台后台统一维护租户资料
"""
    return f"""# {title}

## 推荐使用顺序

1. 先完成系统品牌和基础配置
2. 再配置模型、检索和工具
3. 再导入知识库
4. 再配置智能体和工作流
5. 最后做前台问答测试和日志回看

## 第一次接手这套系统，建议先做什么

- 先确认系统入口能正常打开
- 先完成模型配置，保证问答链路能真正跑起来
- 先上传一小批标准资料做验证，不建议一开始就全量导入
- 先做一个最小可用智能体，把链路跑通之后再扩场景

{platform_block}

## 租户后台怎么用

- 账户管理：维护成员账号、登录信息和访问范围
- 企业定制化：改名称、欢迎语、推荐问题、品牌文案
- 工作流配置：配置默认工作流、草稿、发布和试运行
- 知识库设置：管理知识库、分类、标签、文件上传和文件范围
- Python 脚本设置：放扩展脚本或辅助逻辑
- 模型配置：配置大模型、Embedding、Rerank、Query Rewrite、检索兜底
- 日志查看：看聊天日志、请求日志、护栏事件、采集记录、评测记录
- 问答测试：用真实问题做效果回归

## 前台聊天怎么用

- 用户从 `/login` 登录后进入 `/chat`
- 如果绑定了多个智能体，可以切换不同业务助手
- 前台问题会自动走当前智能体绑定的知识范围、工作流和工具

## 最常见的配置动作

- 新增一个业务助手：先建智能体，再绑工作流，再圈知识范围
- 新增一批资料：先上传文件，再确认分类标签，再验证切片结果
- 调回答效果：先看日志和检索命中，再改模型、Prompt、工作流或知识
- 做客户交付：先完成品牌、欢迎语、推荐问题、默认账号和默认流程

## 检索配置怎么配

- `Hybrid`：向量检索 + BM25 关键词检索一起参与，适合作为默认方案
- `Dense`：只走向量检索，适合先单独验证向量库是否正常
- `BM25`：只走关键词检索，适合精确关键字场景或排查向量链路问题
- 向量库 Provider 可以选 `Qdrant` 或 `Milvus`
- 如果客户要用 Milvus，优先先用 `Dense + Milvus` 做连通性验证，再切回 `Hybrid + Milvus`
- 如果切换了向量库，重点确认知识目录仍在，然后重新做一次问答验证，系统会按当前配置重建索引

## 日常运营建议

- 新增知识时，先按业务范围建好分类和标签，再上传文件
- 调整回答效果时，不要一次改很多项，建议按知识、Prompt、模型、流程分步验证
- 正式上线前，用真实业务问题做一轮问答回归
- 每次大改工作流后，都建议重新跑问答测试和日志回看
"""


def _build_dev_doc(profile: dict) -> str:
    title = "企业版二开与接口说明"
    if profile["key"] == "service_provider":
        title = "服务商版二开与接口说明"
    return f"""# {title}

## 二开时先看哪里

- `backend/main.py`：主要 API 路由和页面入口
- `backend/workflow_runtime.py`：工作流执行核心
- `backend/document_processing.py`：文档解析、切片和导入处理
- `backend/knowledge_assets.py`：知识库结构、标签、文件元数据
- `backend/database.py`：SQLite 表结构与核心数据读写
- `backend/release_profiles.py`：企业版/服务商版裁剪和导出

## 配置文件分层

- `data/app_config.json`：平台级业务配置与版本标识
- `data/model_config.json`：平台级模型配置
- `data/retrieval_config.json`：平台级检索配置
- `data/tool_config.json`：平台级工具与 MCP 配置
- `data/workflow_config.json`：平台级默认工作流模板
- `data/tenants/<tenant_id>/...`：租户级覆盖配置

## 主要接口分组

- 平台管理接口：租户开通、平台日志、平台统计
- 租户后台接口：品牌配置、模型配置、知识管理、工作流管理、问答测试
- 前台接口：登录、会话、聊天、历史消息
- 运维接口：采集、评测、日志查询、版本导出

## 二开时建议遵循的方式

- 先判断需求是平台通用能力，还是某个客户的个性化能力
- 通用能力改母版源码
- 客户个性化内容优先走租户配置、工作流配置和知识目录
- 不建议直接在导出包上长期开发，否则后续发版会越来越乱

## 再次导出企业版和服务商版

```bash
python3 scripts/export_all_releases.py
```

导出结果会固定写到：

- `output/releases/lok_enterprise/`
- `output/releases/lok_service_provider/`
- `output/releases/lok_enterprise.zip`
- `output/releases/lok_service_provider.zip`

## 二开建议

- 优先改母版源码，不要直接在导出包里长期开发
- 平台通用能力优先放 `backend/` 和 `frontend/` 母版
- 客户个性化内容优先走租户配置和知识目录，不要硬编码到通用逻辑
- 新增版本差异时优先补 `backend/release_profiles.py`，避免手工拆包
"""


def _build_ops_doc(profile: dict) -> str:
    title = "企业版运维排障"
    if profile["key"] == "service_provider":
        title = "服务商版运维排障"
    return f"""# {title}

## 日常要看哪些目录

- `data/app.db`：主数据库
- `data/tenants/`：租户配置目录
- `knowledge/`：知识文件目录
- `data/qdrant_store/`：本地向量数据目录
- `data/retrieval_config.json` 或 `data/tenants/<tenant_id>/retrieval_config.json`：检索模式与向量库配置
- `output/releases/`：导出交付目录

## 常见问题怎么查

- 页面打不开：先看服务进程、端口、反向代理和静态文件是否完整
- 登录失败：先看账号是否存在，再看对应租户是否启用
- 问答无结果：先查模型配置、知识切片、检索配置和知识命中日志
- Milvus 不生效：先看 `dense_provider` 是否切到 `milvus`，再看 `uri / token / 用户名密码 / collection / 维度` 是否正确
- Hybrid 结果不对：先单独切到 `Dense` 验证向量库，再看 BM25 权重、Rerank 和知识命中日志
- 工作流报错：先看节点输入输出，再看外部接口和脚本执行日志
- MCP 不生效：先查服务地址、鉴权信息和开关状态

## 建议保留哪些备份

- 每次发版前备份一份完整 `data/`
- 每次大规模改知识前备份 `knowledge/`
- 每次改反向代理或域名配置前备份宿主机配置
- 建议保留至少最近一个稳定版本的整包备份

## 升级前备份什么

- `data/`
- `knowledge/`
- `.env` 与密钥配置
- Nginx 或进程托管配置

## 升级后重点验证什么

- 登录、聊天、后台入口是否正常
- 租户配置是否还在
- 知识检索是否还能命中
- 工作流是否还能跑通
- 日志和统计是否正常写入

## 交付包为什么不带真实数据

- 防止把客户密钥、正式数据库、正式向量索引直接带出
- 方便你在新环境按标准步骤重新初始化
- 降低把母版测试数据误交付给客户的风险
"""


def _build_vector_store_doc(profile: dict) -> str:
    title = "企业版检索与向量库说明"
    if profile["key"] == "service_provider":
        title = "服务商版检索与向量库说明"
    return f"""# {title}

## 当前交付包支持什么

- 检索模式支持 `Hybrid`、`Dense`、`BM25`
- Dense 向量库支持 `Qdrant` 和 `Milvus`
- 知识文件始终保留在 `knowledge/` 目录
- Qdrant / Milvus 负责向量索引，切换向量库时不需要重新上传知识文件

## 三种检索模式怎么理解

- `Hybrid`：向量检索 + BM25，一般建议作为默认生产方案
- `Dense`：只用向量检索，适合排查向量链路和验证 Qdrant / Milvus 连接
- `BM25`：只用关键词检索，适合精确关键字场景或做兜底排查

## Qdrant 和 Milvus 怎么选

- 如果客户已经有 Milvus 基础设施，直接用 `Milvus` 即可
- 如果客户更偏本地嵌入式或已有 Qdrant 使用经验，可以继续用 `Qdrant`
- 如果只是本机验证功能，Milvus 可以先用本地文件路径做 Lite 模式验证
- 正式环境优先使用客户自己的远程向量服务，不建议长期把测试用本地文件模式当生产方案

## 推荐验证顺序

1. 先确认知识文件已经导入并完成切片
2. 先用 `Dense + Qdrant` 或 `Dense + Milvus` 验证单向量链路
3. 问一个知识库中明确有答案的问题，确认能命中
4. 再切回 `Hybrid`，确认 BM25 与向量检索都能参与
5. 最后再绑定正式智能体和工作流做整链路回归

## 切换到 Milvus 时要注意什么

- 检查 `dense_provider` 是否已经切成 `milvus`
- 检查 `uri`、`token`、`user`、`password`、`db_name`、`collection`
- 检查 `vector_size` 是否和当前 Embedding 维度一致
- 第一次问答如果稍慢，通常是在根据现有知识目录重建索引

## 交付时怎么跟客户说明

- 当前交付包已经内置 Milvus 支持，不需要额外改代码
- 客户只需要在后台切换向量库 Provider 并填写自己的 Milvus 连接信息
- 如果客户后续从 Qdrant 切到 Milvus，本质上是重建索引，不是重传知识文件
"""


def _write_release_docs(bundle_dir: Path, profile: dict) -> None:
    docs_dir = bundle_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    (bundle_dir / "README.md").write_text(_build_release_readme(profile), encoding="utf-8")
    (docs_dir / "README.md").write_text(_build_docs_index(profile), encoding="utf-8")
    (docs_dir / "00_版本与交付说明.md").write_text(_build_version_doc(profile), encoding="utf-8")
    (docs_dir / "01_功能介绍.md").write_text(_build_feature_intro_doc(profile), encoding="utf-8")
    (docs_dir / "02_部署与启动.md").write_text(_build_deploy_doc(profile), encoding="utf-8")
    (docs_dir / "03_使用教程.md").write_text(_build_tutorial_doc(profile), encoding="utf-8")
    (docs_dir / "04_二开与接口说明.md").write_text(_build_dev_doc(profile), encoding="utf-8")
    (docs_dir / "05_运维排障.md").write_text(_build_ops_doc(profile), encoding="utf-8")
    (docs_dir / "06_检索与向量库说明.md").write_text(_build_vector_store_doc(profile), encoding="utf-8")


def _safe_json_load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _safe_json_dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _sanitize_model_config(path: Path) -> None:
    if not path.exists():
        return
    data = _safe_json_load(path)
    if not data:
        return
    data.pop("api_keys", None)
    data["base_url"] = ""
    data["model_primary"] = ""
    data["model_fallback"] = ""
    providers = []
    for item in list(data.get("providers") or []):
        if not isinstance(item, dict):
            continue
        providers.append(
            {
                "id": str(item.get("id") or "").strip() or "provider_1",
                "label": "",
                "base_url": "",
                "model_primary": "",
                "model_fallback": "",
                "api_keys": [],
            }
        )
    data["providers"] = providers
    _safe_json_dump(path, data)


def _sanitize_retrieval_config(path: Path) -> None:
    if not path.exists():
        return
    data = _safe_json_load(path)
    if not data:
        return
    qdrant = dict(data.get("qdrant") or {})
    qdrant.update(SAFE_QDRANT_PLACEHOLDER)
    data["qdrant"] = qdrant
    milvus = dict(data.get("milvus") or {})
    milvus.update(SAFE_MILVUS_PLACEHOLDER)
    data["milvus"] = milvus
    embedding = dict(data.get("embedding") or {})
    embedding["api_key"] = ""
    embedding["base_url"] = str(embedding.get("base_url") or "")
    data["embedding"] = embedding
    rerank = dict(data.get("rerank") or {})
    rerank["api_key"] = ""
    rerank["base_url"] = str(rerank.get("base_url") or "")
    data["rerank"] = rerank
    _safe_json_dump(path, data)


def _sanitize_app_config(path: Path) -> None:
    if not path.exists():
        return
    data = _safe_json_load(path)
    if not data:
        return
    for key, value in SAFE_APP_CONFIG_TEMPLATE_FIELDS.items():
        data[key] = copy.deepcopy(value)
    _safe_json_dump(path, data)


def _sanitize_workflow_config(path: Path) -> None:
    if path.exists():
        _safe_json_dump(path, copy.deepcopy(SAFE_WORKFLOW_TEMPLATE))


def _sanitize_service_provider_sample(bundle_dir: Path) -> None:
    tenant_root = bundle_dir / "data" / "tenants" / SERVICE_PROVIDER_SAMPLE_TENANT_ID
    knowledge_root = bundle_dir / "knowledge" / SERVICE_PROVIDER_SAMPLE_TENANT_ID
    source_tenant_root = BASE_DIR / "data" / "tenants" / SERVICE_PROVIDER_SAMPLE_TENANT_ID
    source_knowledge_root = BASE_DIR / "knowledge" / SERVICE_PROVIDER_SAMPLE_TENANT_ID
    if not source_tenant_root.exists() or not source_knowledge_root.exists():
        return

    shutil.rmtree(tenant_root, ignore_errors=True)
    shutil.rmtree(knowledge_root, ignore_errors=True)
    shutil.copytree(source_tenant_root, tenant_root)
    shutil.copytree(source_knowledge_root, knowledge_root)

    (tenant_root / "api_keys.txt").unlink(missing_ok=True)
    (tenant_root / "demo_setup_summary.json").unlink(missing_ok=True)

    _sanitize_model_config(tenant_root / "model_config.json")
    _sanitize_retrieval_config(tenant_root / "retrieval_config.json")

    app_config = _safe_json_load(tenant_root / "app_config.json")
    if app_config:
        app_config["edition"] = "service_provider"
        app_config["deployment_mode"] = "double_backend"
        app_config["feature_flags"] = copy.deepcopy(RELEASE_PROFILES["service_provider"]["features"])
        app_config["feature_flags"]["factory_center"] = False
        app_config["feature_flags"]["release_export"] = False
        app_config["factory_enabled"] = False
        app_config["login_hint"] = "医院租户测试账号登录 · 支持多个智能体独立入口与统一切换"
        _safe_json_dump(tenant_root / "app_config.json", app_config)

    tool_config = _safe_json_load(tenant_root / "tool_config.json")
    mcp = dict(tool_config.get("mcp") or {})
    servers = []
    for item in list(mcp.get("servers") or []):
        if not isinstance(item, dict):
            continue
        clean_item = copy.deepcopy(item)
        clean_item["bridge_url"] = ""
        clean_item["auth_token"] = ""
        clean_item["enabled"] = False
        servers.append(clean_item)
    mcp["servers"] = servers
    mcp["enabled"] = False
    tool_config["mcp"] = mcp
    _safe_json_dump(tenant_root / "tool_config.json", tool_config)

    metadata = _safe_json_load(tenant_root / "knowledge_metadata.json")
    if metadata:
        payload = json.dumps(metadata, ensure_ascii=False)
        payload = payload.replace("演示模板", "默认模板")
        tenant_root.joinpath("knowledge_metadata.json").write_text(payload, encoding="utf-8")

    _build_service_provider_sample_db(bundle_dir)


def _build_service_provider_sample_db(bundle_dir: Path) -> None:
    source_db = BASE_DIR / "data" / "app.db"
    target_db = bundle_dir / "data" / "app.db"
    if not source_db.exists():
        return
    target_db.parent.mkdir(parents=True, exist_ok=True)

    source_conn = sqlite3.connect(source_db)
    source_conn.row_factory = sqlite3.Row
    target_conn = sqlite3.connect(target_db)
    try:
        target_conn.executescript(
            """
            CREATE TABLE tenants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT UNIQUE NOT NULL,
                tenant_name TEXT NOT NULL,
                admin_username TEXT UNIQUE NOT NULL,
                admin_password_hash TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE phone_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT DEFAULT 'default',
                phone TEXT UNIQUE NOT NULL,
                display_name TEXT DEFAULT '',
                password_hash TEXT DEFAULT NULL,
                must_change_password INTEGER DEFAULT 1,
                device_a TEXT DEFAULT NULL,
                device_b TEXT DEFAULT NULL,
                balance INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                last_login TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'draft',
                enabled INTEGER DEFAULT 1,
                avatar TEXT DEFAULT '',
                welcome_message TEXT DEFAULT '',
                input_placeholder TEXT DEFAULT '',
                recommended_questions TEXT DEFAULT '[]',
                prompt_override TEXT DEFAULT '',
                workflow_id TEXT DEFAULT '',
                knowledge_scope TEXT DEFAULT '{}',
                model_override TEXT DEFAULT '{}',
                tool_scope TEXT DEFAULT '[]',
                mcp_servers TEXT DEFAULT '[]',
                streaming INTEGER DEFAULT 1,
                fallback_enabled INTEGER DEFAULT 1,
                fallback_message TEXT DEFAULT '',
                show_recommended INTEGER DEFAULT 1,
                is_default INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, agent_id)
            );
            CREATE TABLE agent_user_bindings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                phone TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(tenant_id, agent_id, phone)
            );
            CREATE TABLE chat_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT UNIQUE NOT NULL,
                tenant_id TEXT DEFAULT 'default',
                agent_id TEXT DEFAULT '',
                phone TEXT NOT NULL,
                title TEXT DEFAULT '新对话',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE chat_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT DEFAULT '',
                session_id TEXT DEFAULT '',
                tenant_id TEXT DEFAULT 'default',
                agent_id TEXT DEFAULT '',
                phone TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                knowledge_hits TEXT DEFAULT '[]',
                retrieval_trace TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE request_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                tenant_id TEXT DEFAULT 'default',
                path TEXT NOT NULL,
                method TEXT NOT NULL,
                status_code INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                client_ip TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                cache_status TEXT DEFAULT '',
                model_name TEXT DEFAULT '',
                error_message TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE guardrail_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                phone TEXT DEFAULT '',
                stage TEXT NOT NULL,
                action TEXT NOT NULL,
                rule_name TEXT NOT NULL,
                detail TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tenant_id TEXT DEFAULT 'default'
            );
            CREATE TABLE crawler_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT DEFAULT 'default',
                source_id TEXT NOT NULL,
                source_name TEXT NOT NULL,
                status TEXT NOT NULL,
                tier TEXT DEFAULT '',
                items_count INTEGER DEFAULT 0,
                detail TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE evaluation_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT DEFAULT 'default',
                name TEXT NOT NULL,
                total_questions INTEGER DEFAULT 0,
                hit_at_1 INTEGER DEFAULT 0,
                hit_at_3 INTEGER DEFAULT 0,
                hit_at_5 INTEGER DEFAULT 0,
                avg_top_score REAL DEFAULT 0,
                detail TEXT DEFAULT '[]',
                config_snapshot TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        tenant_row = source_conn.execute(
            "SELECT tenant_id, tenant_name, admin_username, admin_password_hash, enabled, created_at FROM tenants WHERE tenant_id = ?",
            (SERVICE_PROVIDER_SAMPLE_TENANT_ID,),
        ).fetchone()
        if tenant_row:
            target_conn.execute(
                """
                INSERT INTO tenants (tenant_id, tenant_name, admin_username, admin_password_hash, enabled, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                tuple(tenant_row),
            )

        phone_rows = source_conn.execute(
            """
            SELECT tenant_id, phone, display_name, password_hash, must_change_password, device_a, device_b, balance, enabled, last_login, created_at
            FROM phone_accounts
            WHERE tenant_id = ?
            """,
            (SERVICE_PROVIDER_SAMPLE_TENANT_ID,),
        ).fetchall()
        for row in phone_rows:
            values = list(row)
            if values[2] == "华东协同医院演示账号":
                values[2] = "华东协同医院测试账号"
            target_conn.execute(
                """
                INSERT INTO phone_accounts (
                    tenant_id, phone, display_name, password_hash, must_change_password,
                    device_a, device_b, balance, enabled, last_login, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

        agent_rows = source_conn.execute(
            """
            SELECT tenant_id, agent_id, name, description, status, enabled, avatar, welcome_message,
                   input_placeholder, recommended_questions, prompt_override, workflow_id,
                   knowledge_scope, model_override, tool_scope, mcp_servers, streaming,
                   fallback_enabled, fallback_message, show_recommended, is_default, created_at, updated_at
            FROM agents
            WHERE tenant_id = ?
            ORDER BY id
            """,
            (SERVICE_PROVIDER_SAMPLE_TENANT_ID,),
        ).fetchall()
        for row in agent_rows:
            values = list(row)
            values[13] = "{}"
            target_conn.execute(
                """
                INSERT INTO agents (
                    tenant_id, agent_id, name, description, status, enabled, avatar, welcome_message,
                    input_placeholder, recommended_questions, prompt_override, workflow_id,
                    knowledge_scope, model_override, tool_scope, mcp_servers, streaming,
                    fallback_enabled, fallback_message, show_recommended, is_default, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                values,
            )

        binding_rows = source_conn.execute(
            "SELECT tenant_id, agent_id, phone, created_at FROM agent_user_bindings WHERE tenant_id = ?",
            (SERVICE_PROVIDER_SAMPLE_TENANT_ID,),
        ).fetchall()
        for row in binding_rows:
            target_conn.execute(
                "INSERT INTO agent_user_bindings (tenant_id, agent_id, phone, created_at) VALUES (?, ?, ?, ?)",
                tuple(row),
            )

        target_conn.commit()
    finally:
        source_conn.close()
        target_conn.close()


def _sanitize_bundle_configs(bundle_dir: Path) -> None:
    for env_path in [bundle_dir / ".env", bundle_dir / ".env.local", bundle_dir / ".env.production"]:
        if env_path.exists():
            env_path.unlink()

    sensitive_files = [
        bundle_dir / "config" / "api_keys.txt",
        bundle_dir / "data" / "app.db",
        bundle_dir / "data" / "app.db-shm",
        bundle_dir / "data" / "app.db-wal",
        bundle_dir / "output" / "授权码_100个.xlsx",
    ]
    for path in sensitive_files:
        if path.exists():
            path.unlink()

    shutil.rmtree(bundle_dir / "data" / "qdrant_store", ignore_errors=True)
    shutil.rmtree(bundle_dir / "data" / "qdrant_store_template", ignore_errors=True)

    for tenant_key_file in (bundle_dir / "data" / "tenants").glob("*/api_keys.txt"):
        tenant_key_file.unlink(missing_ok=True)

    knowledge_root = bundle_dir / "knowledge"
    for tenant_dir in knowledge_root.iterdir() if knowledge_root.exists() else []:
        if not tenant_dir.is_dir():
            continue
        if tenant_dir.name == "default":
            continue
        shutil.rmtree(tenant_dir, ignore_errors=True)

    tenants_root = bundle_dir / "data" / "tenants"
    for tenant_dir in tenants_root.iterdir() if tenants_root.exists() else []:
        if not tenant_dir.is_dir():
            continue
        if tenant_dir.name == "default":
            continue
        shutil.rmtree(tenant_dir, ignore_errors=True)

    _sanitize_app_config(bundle_dir / "data" / "app_config.json")
    _sanitize_model_config(bundle_dir / "data" / "model_config.json")
    _sanitize_retrieval_config(bundle_dir / "data" / "retrieval_config.json")
    _sanitize_workflow_config(bundle_dir / "data" / "workflow_config.json")
    _sanitize_app_config(bundle_dir / "data" / "tenants" / "default" / "app_config.json")
    _sanitize_model_config(bundle_dir / "data" / "tenants" / "default" / "model_config.json")
    _sanitize_retrieval_config(bundle_dir / "data" / "tenants" / "default" / "retrieval_config.json")
    _sanitize_workflow_config(bundle_dir / "data" / "tenants" / "default" / "workflow_config.json")


def _remove_markdown_except_release_docs(bundle_dir: Path) -> None:
    keep = {
        bundle_dir / "README.md",
        bundle_dir / "docs" / "README.md",
        bundle_dir / "docs" / "00_版本与交付说明.md",
        bundle_dir / "docs" / "01_功能介绍.md",
        bundle_dir / "docs" / "02_部署与启动.md",
        bundle_dir / "docs" / "03_使用教程.md",
        bundle_dir / "docs" / "04_二开与接口说明.md",
        bundle_dir / "docs" / "05_运维排障.md",
        bundle_dir / "docs" / "06_检索与向量库说明.md",
    }
    for md_path in bundle_dir.rglob("*.md"):
        if "knowledge" in md_path.parts:
            continue
        if md_path.name == "system_prompt.md":
            continue
        if md_path in keep:
            continue
        md_path.unlink(missing_ok=True)


def _cleanup_bundle_by_profile(bundle_dir: Path, profile: dict) -> None:
    cleanup_targets = [
        bundle_dir / "backups",
        bundle_dir / "newUI",
        bundle_dir / "seed_demo_agents.py",
        bundle_dir / "tmp",
        bundle_dir / "xianyu",
        bundle_dir / "test.txt",
        bundle_dir / "source_catalog.json",
        bundle_dir / ".env.example",
        bundle_dir / "config" / "api_keys.txt.example",
        bundle_dir / "data" / "tenants" / "acme_acceptance",
        bundle_dir / "data" / "tenants" / "beta_acceptance",
        bundle_dir / "data" / "tenants" / "gamma_acceptance",
        bundle_dir / "data" / "tenants" / "smoke_enterprise",
        bundle_dir / "knowledge" / "acme_acceptance",
        bundle_dir / "knowledge" / "smoke_enterprise",
        bundle_dir / "frontend" / "tenant_v2.html.bak2",
        bundle_dir / "backend" / "hospital_mock.py",
        bundle_dir / "scripts" / "setup_health_check_demo.py",
        bundle_dir / "scripts" / "setup_hospital_multi_agent_demo.py",
        bundle_dir / "scripts" / "prod_recover_shared_data.sh",
        bundle_dir / "docs" / "demo-tenants.md",
    ]
    cleanup_targets.extend(bundle_dir.glob("tmp_*.py"))
    allowed_frontend = set(profile.get("frontend_files") or [])
    frontend_dir = bundle_dir / "frontend"
    for page in frontend_dir.glob("*.html"):
        if page.name not in allowed_frontend:
            cleanup_targets.append(page)
    for target in cleanup_targets:
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
    for ds_store in bundle_dir.rglob(".DS_Store"):
        ds_store.unlink(missing_ok=True)


def _create_release_zip(bundle_dir: Path, zip_name: str) -> Path:
    zip_path = OUTPUT_DIR / zip_name
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(bundle_dir):
            for file in files:
                if file == ".DS_Store":
                    continue
                abs_path = Path(root) / file
                rel_path = abs_path.relative_to(bundle_dir.parent)
                zf.write(abs_path, rel_path.as_posix())
    return zip_path


def _cleanup_legacy_release_artifacts() -> None:
    if not OUTPUT_DIR.exists():
        return
    legacy_prefixes = ("lok_enterprise_", "lok_service_provider_")
    stable_names = {
        "lok_enterprise",
        "lok_service_provider",
        "lok_enterprise.zip",
        "lok_service_provider.zip",
    }
    for path in OUTPUT_DIR.iterdir():
        if path.name in stable_names:
            continue
        if not path.name.startswith(legacy_prefixes):
            continue
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def export_release_bundle(*, profile_key: str, current_app_config: dict) -> dict:
    profile = get_release_profile(profile_key)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    _cleanup_legacy_release_artifacts()

    bundle_name = f"lok_{profile_key}"
    bundle_dir = OUTPUT_DIR / bundle_name
    shutil.rmtree(bundle_dir, ignore_errors=True)

    ignore = shutil.ignore_patterns(
        ".git",
        ".DS_Store",
        "__pycache__",
        "venv",
        "output",
        "output/releases",
        "*.pyc",
        "*.pyo",
        "*.bak",
        "*.bak2",
        ".codex_write_test*",
        ".env",
        ".env.local",
        ".env.production",
        "api_keys.txt",
        "app.db",
        "app.db-shm",
        "app.db-wal",
        "qdrant_store",
        "qdrant_store_template",
    )
    shutil.copytree(BASE_DIR, bundle_dir, dirs_exist_ok=False, ignore=ignore)

    app_config_path = bundle_dir / "data" / "app_config.json"
    app_config = build_release_app_config(profile=profile, current_app_config=current_app_config)
    app_config_path.write_text(json.dumps(app_config, ensure_ascii=False, indent=2), encoding="utf-8")

    _cleanup_bundle_by_profile(bundle_dir, profile)
    for cache_dir in bundle_dir.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)
    _sanitize_bundle_configs(bundle_dir)
    _safe_json_dump(app_config_path, build_release_app_config(profile=profile, current_app_config=_safe_json_load(app_config_path)))
    (bundle_dir / "data" / "qdrant_store_template").mkdir(parents=True, exist_ok=True)
    (bundle_dir / "data" / "qdrant_store_template" / "README.txt").write_text(
        "该目录为本地向量库占位目录，正式部署后会在此生成本地检索数据，请勿提交真实索引数据。",
        encoding="utf-8",
    )

    _write_release_docs(bundle_dir, profile)
    _remove_markdown_except_release_docs(bundle_dir)
    export_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _safe_json_dump(
        bundle_dir / "release_manifest.json",
        {
            "profile_key": profile["key"],
            "label": profile["label"],
            "deployment_mode": profile["deployment_mode"],
            "exported_at": export_time,
            "frontend_files": profile.get("frontend_files") or [],
            "docs": [
                "README.md",
                "docs/README.md",
                "docs/00_版本与交付说明.md",
                "docs/01_功能介绍.md",
                "docs/02_部署与启动.md",
                "docs/03_使用教程.md",
                "docs/04_二开与接口说明.md",
                "docs/05_运维排障.md",
                "docs/06_检索与向量库说明.md",
            ],
        },
    )
    zip_path = _create_release_zip(bundle_dir, f"lok_{profile_key}.zip")

    return {
        "profile": profile,
        "bundle_dir": str(bundle_dir),
        "zip_path": str(zip_path),
        "zip_name": zip_path.name,
    }


def export_all_release_bundles(*, current_app_config: dict) -> list[dict]:
    results = []
    for profile_key in RELEASE_PROFILES:
        results.append(export_release_bundle(profile_key=profile_key, current_app_config=current_app_config))
    return results
