# 企业级智能问答鸿蒙App

## 项目简介

这是基于多租户企业智能体平台开发的鸿蒙原生应用，支持：
- 多租户账号登录
- 智能体选择与切换
- 知识库问答
- 流式对话体验
- 语音交互（鸿蒙语音能力）
- 跨设备同步

## 参赛亮点

1. **应用智能化创新** - 集成AI大模型，支持自然语言对话和知识库检索
2. **全场景一体化** - 支持手机、平板、PC等多设备协同
3. **全新交互形态** - Agent智能体+语音交互
4. **安全隐私保护** - 多租户数据隔离

## 技术栈

- **HarmonyOS NEXT**
- **ArkTS + ArkUI**
- **HTTP请求** - 对接现有FastAPI后端
- **语音服务** - 华为语音识别与合成
- **分布式数据管理** - 跨设备同步

## 项目结构

```
harmony_app/
├── entry/
│   └── src/
│       └── main/
│           ├── ets/
│           │   ├── pages/
│           │   │   ├── Index.ets          # 首页/登录
│           │   │   ├── Chat.ets           # 聊天页面
│           │   │   └── AgentSelect.ets    # 智能体选择
│           │   ├── services/
│           │   │   ├── ApiService.ets     # API调用封装
│           │   │   └── ChatService.ets    # 聊天服务
│           │   ├── models/
│           │   │   ├── Agent.ets          # 智能体模型
│           │   │   └── Message.ets        # 消息模型
│           │   └── utils/
│           │       └── Constants.ets      # 常量定义
│           └── module.json5
└── build-profile.json5
```

## 快速开始

1. 安装 DevEco Studio
2. 导入项目
3. 配置后端地址（`Constants.ets`）
4. 连接设备或模拟器运行

## 后端对接

确保后端服务运行在：
```
http://localhost:6090
```

或修改 `Constants.ets` 中的 `BASE_URL`。
