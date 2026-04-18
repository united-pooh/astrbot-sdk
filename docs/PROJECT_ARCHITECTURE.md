# AstrBot SDK 架构概述文档

> 作者：whatevertogo
> 最后更新：2026-03-27

---

## 目录

1. [项目概述](#项目概述)
2. [核心架构层次](#核心架构层次)
3. [协议层设计](#协议层设计)
4. [运行时架构](#运行时架构)
5. [客户端层设计](#客户端层设计)
6. [关键设计模式](#关键设计模式)
7. [文档与资源](#文档与资源)

---

## 项目概述

AstrBot SDK 是一个基于 Python 3.12+ 的机器人插件开发框架，采用**Worker 隔离**和**能力路由**架构，支持插件的动态加载、独立运行和跨进程通信。

### 核心特性

| 特性 | 描述 |
|------|------|
| **Worker 隔离** | Worker 可单插件运行，也可由 GroupWorkerRuntime 承载多个兼容插件；单个 Worker 崩溃不会影响其他 Worker |
| **环境分组** | 多插件可共享同一 Python 虚拟环境，节省资源 |
| **能力路由** | 显式声明的 Capability 系统，支持 JSON Schema 验证 |
| **流式支持** | 原生支持流式 LLM 调用和增量结果返回 |
| **协议优先** | 基于 s5r 协议的统一通信模型，支持 Stdio/WebSocket 等多种传输方式 |

### 技术栈

- **Python**: 3.12+
- **异步框架**: asyncio
- **Web 框架**: aiohttp
- **数据验证**: pydantic
- **日志**: loguru
- **配置**: pyyaml
- **LLM**: openai, anthropic, google-genai
- **包管理**: uv (环境分组)

---

## 核心架构层次

```
┌─────────────────────────────────────────────────────────────────┐
│                   用户层 (Plugin Developer)                    │
├─────────────────────────────────────────────────────────────────┤
│  astrbot-sdk 入口:  astrbot_sdk.{Star, Context, MessageEvent}  │
│  消息链:   MessageChain, MessageBuilder, MessageEventResult    │
│  消息组件: Plain, Image, At, AtAll, File, Video, Record, ...   │
│  触发器:   on_command, on_message, on_event, on_schedule,      │
│           conversation_command                                  │
│  权限:     require_admin, admin_only, require_permission,      │
│           platforms, group_only, private_only                   │
│  限流:     rate_limit, cooldown                                 │
│  能力导出: provide_capability, register_llm_tool,              │
│           register_agent, http_api, register_skill             │
│  其他:     priority, validate_config, on_provider_change,      │
│           background_task                                       │
│  过滤器:   PlatformFilter, MessageTypeFilter, CustomFilter,    │
│           all_of, any_of, custom_filter                         │
│  会话:     MessageSession, session_waiter, SessionController   │
│  工具:     StarTools, PluginKVStoreMixin, CommandGroup         │
│  对话:     ConversationSession, ConversationState              │
└────────────────────┬────────────────────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────────────────────┐
│                 高层 API (High-Level API)                      │
├─────────────────────────────────────────────────────────────────┤
│  能力客户端 (通过 CapabilityProxy → Peer → Transport 调用):    │
│    LLMClient / MemoryClient / DBClient / PlatformClient        │
│    HTTPClient / MetadataClient                                 │
│    以及管理类客户端 (conversation, persona, kb, provider, ...)  │
└────────────────────┬────────────────────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────────────────────┐
│              执行边界 (Execution Boundary)                     │
├─────────────────────────────────────────────────────────────────┤
│  runtime 主干:                                                 │
│    - loader.py              (插件发现、加载、环境管理)         │
│    - bootstrap.py           (Supervisor/Worker 启动)           │
│    - handler_dispatcher.py  (Handler 执行分发、参数注入)       │
│    - capability_dispatcher.py (Capability 调用分发)            │
│    - capability_router.py   (Capability 路由、Schema 验证)     │
│    - peer.py                (协议对等端)                       │
│    - transport.py           (传输抽象)                         │
└────────────────────┬────────────────────────────────────────────┘
                   │
┌──────────────────▼─────────────────────────────────────────────┐
│             协议与传输 (Protocol & Transport)                  │
├─────────────────────────────────────────────────────────────────┤
│  protocol/                                                     │
│    - messages.py          (协议消息模型)                       │
│    - descriptors.py       (Handler/Capability 描述符)          │
│  transport 实现:                                               │
│    - StdioTransport            (标准输入输出)                  │
│    - WebSocketServerTransport  (WebSocket 服务端)              │
│    - WebSocketClientTransport  (WebSocket 客户端)              │
└─────────────────────────────────────────────────────────────────┘
```

### 层次职责

| 层次 | 职责 | 主要模块 |
|------|------|---------|
| **用户层** | 插件开发者 API | `Star`, `Context`, `MessageEvent`, 装饰器, 过滤器 |
| **高层 API** | 类型化的能力客户端 | `clients/{llm, memory, db, platform, http, metadata}` |
| **执行边界** | 插件加载、路由、分发 | `runtime/loader.py`, `runtime/*_dispatcher.py` |
| **协议层** | 消息模型、描述符、JSON Schema | `protocol/` |
| **传输层** | 底层通信抽象 | `runtime/transport.py` |

### 核心设计原则

1. **延迟加载**：`runtime/__init__.py` 使用 `__getattr__` 避免导入时加载重型依赖
2. **插件身份透传**：通过 `caller_plugin_scope()` 上下文管理器将 plugin_id 注入协议层
3. **声明式优先**：所有配置都是数据结构（描述符），便于序列化和跨进程传递
4. **类型安全**：使用 Pydantic 模型和类型注解提供验证和 IDE 支持

---

## 协议层设计

### 消息模型

s5r 协议定义了 5 种消息类型：

| 消息类型 | 用途 | 关键字段 |
|---------|------|---------|
| `InitializeMessage` | 握手初始化 | `protocol_version`, `peer`, `handlers`, `provided_capabilities`, `metadata`（可选）|
| `InvokeMessage` | 调用能力 | `capability`, `input`, `stream`, `caller_plugin_id` |
| `ResultMessage` | 返回结果 | `success`, `output`, `error`, `kind` |
| `EventMessage` | 流式事件 | `phase` (started/delta/completed/failed), `data`, `output`（completed 时）, `error`（failed 时）|
| `CancelMessage` | 取消调用 | `reason` |

### 错误模型

`ErrorPayload` 使用字符串 code（而非整数），包含：
- `code`: 错误码（如 "capability_not_found"）
- `message`: 开发者信息
- `hint`: 用户友好提示
- `retryable`: 是否可重试
- `docs_url`: （可选）文档链接
- `details`: （可选）结构化调试详情

### 握手流程

```
Worker (Plugin)                 Supervisor (Core)
     |                               |
     |  InitializeMessage             |
     |  (handlers, capabilities)      |
     |----------------------------->|
     |                               |
     |  ResultMessage(kind="init")   |
     |<-----------------------------|
     |                               |
     |  InvokeMessage(handler.invoke)  |
     |<-----------------------------|
     |  执行用户 handler             |
     |                               |
     |  ResultMessage(output)         |
     |----------------------------->|
```

### 描述符模型

#### HandlerDescriptor

```python
{
    "id": "plugin.module:handler_name",
    "trigger": {
        "type": "command",
        "command": "hello",
        "aliases": ["hi"],
        "description": "打招呼命令"
    },
    "kind": "handler",           # handler | hook | tool | session
    "contract": "message_event", # message_event | schedule
    "priority": 0,
    "permissions": {"require_admin": False, "level": 0},
    "filters": [],
    "param_specs": []
}
```

#### Trigger 类型

| 类型 | 关键字段 | 说明 |
|------|---------|------|
| `CommandTrigger` | command, aliases, platforms | 命令触发 |
| `MessageTrigger` | regex, keywords, platforms | 消息触发（正则/关键词） |
| `EventTrigger` | event_type | 事件触发 |
| `ScheduleTrigger` | cron, interval_seconds | 定时触发 |

---

## 运行时架构

### 组件关系图

```
                    ┌──────────────┐
                    │  AstrBot     │
                    │    Core      │
                    └──────┬───────┘
                           │
                    ┌──────▼───────┐
                    │ Supervisor   │
                    │  Runtime     │
                    └──────┬───────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
  ┌─────▼─────┐    ┌──────▼────┐   ┌────────▼───┐
  │   Peer     │                   │   Peer    │              │   Peer      │
  │  (stdio)   │                   │  (stdio)  │              │  (ws)       │
  └─────┬─────┘    └─────┬─────┘   └─────┬──────┘
        │                  │                │
  ┌─────▼─────┐    ┌──────▼────┐   ┌──────▼─────┐
  │  Worker    │                   │  Worker   │            │  Worker     │
  │  Runtime   │                   │  Runtime  │            │  Runtime    │
  └─────┬─────┘    └─────┬─────┘   └─────┬──────┘
        │                  │                │
  ┌─────▼─────┐    ┌──────▼────┐   ┌──────▼─────┐
  │  Plugin A  │                     │ Plugin B  │             │ Plugin C+D  │
  │            │                     │           │             │ (Group)     │
  └───────────┘    └───────────┘   └─────────────┘
```

### 核心运行时组件

| 组件 | 职责 |
|------|------|
| **SupervisorRuntime** | 管理多个 Worker 进程，聚合所有 handler，路由 Core 调用到对应 Worker |
| **WorkerSession** | 管理单个 Worker 进程的生命周期，处理连接关闭和重连 |
| **PluginWorkerRuntime** | Worker 进程内加载单个插件，分发 handler 调用 |
| **GroupWorkerRuntime** | 在同一 Worker 进程中承载多个兼容插件，聚合 handlers 和 capabilities |
| **HandlerDispatcher** | 将 handler.invoke 请求转成真实 Python 调用，支持参数注入 |
| **CapabilityDispatcher** | 插件提供的能力调用分发 |
| **CapabilityRouter** | 能力注册、发现和执行路由，支持 JSON Schema 验证 |
| **EnvironmentGroup** | 环境分组数据结构，支持多插件共享同一 Python 环境 |
| **PluginEnvironmentManager** | 插件环境管理和规划，支持环境分组策略 |

### 参数注入优先级

HandlerDispatcher 支持参数注入，优先级为：

1. **按类型注解注入**（`MessageEvent`, `Context`）
2. **按参数名注入**（`event`, `ctx`, `context`）
3. **从 legacy_args 注入**（命令参数等）

---

## 客户端层设计

### 调用链路

```
┌─────────────────────────────────────────────────────────────┐
│                    User Plugin                              │
│  ctx.llm.chat() / ctx.memory.save() / ctx.db.set()         │
└────────────┬────────────────────────────────────────────────┘
             │
┌────────────▼────────────────────────────────────────────────┐
│               CapabilityProxy                               │
│  - call(name, payload)      普通调用                        │
│  - stream(name, payload)    流式调用                        │
└────────────┬────────────────────────────────────────────────┘
             │
┌────────────▼────────────────────────────────────────────────┐
│                    Peer                                     │
│  - invoke(capability, payload)                              │
│  - invoke_stream(capability, payload)                       │
└────────────┬────────────────────────────────────────────────┘
             │
┌────────────▼────────────────────────────────────────────────┐
│                 Transport                                   │
│  - send(json_string)                                       │
└─────────────────────────────────────────────────────────────┘
```

### 客户端一览

> 完整 API 参考：`src/astrbot_sdk/context.py`（属性定义）、`src/astrbot_sdk/clients/`（客户端实现）。

| Context 属性 | 客户端类 | 对应 Capability 命名空间 |
|-------------|---------|------------------------|
| `ctx.llm` | `LLMClient` | `llm.*` |
| `ctx.memory` | `MemoryClient` | `memory.*` |
| `ctx.db` | `DBClient` | `db.*` |
| `ctx.platform` | `PlatformClient` | `platform.*` |
| `ctx.http` | `HTTPClient` | `http.*` |
| `ctx.metadata` | `MetadataClient` | `metadata.*` |
| `ctx.message_history` | `MessageHistoryManagerClient` | `message_history.*` |
| `ctx.conversations` | `ConversationManagerClient` | `conversation.*` |
| `ctx.personas` | `PersonaManagerClient` | `persona.*` |
| `ctx.kbs` | `KnowledgeBaseManagerClient` | `kb.*` |
| `ctx.providers` | `ProviderClient` | `provider.*` |
| `ctx.provider_manager` | `ProviderManagerClient` | `provider.manager.*` |
| `ctx.permission` | `PermissionClient` | `permission.*` |
| `ctx.permission_manager` | `PermissionManagerClient` | `permission.manager.*` |
| `ctx.skills` | `SkillClient` | `skill.*` |
| `ctx.session_plugins` | `SessionPluginManager` | `session.plugin.*` |
| `ctx.session_services` | `SessionServiceManager` | `session.service.*` |
| `ctx.registry` | `RegistryClient` | 内部使用 |

---

## 关键设计模式

### 1. 协议优先模式

- 所有跨进程通信都通过 s5r 协议
- 传输层只处理字符串，协议由 Peer 层处理
- 支持多种传输方式（Stdio, WebSocket）

### 2. 能力路由模式

- 显式声明 Capability 和输入/输出 Schema
- 通过 CapabilityRouter 统一路由
- 支持同步和流式两种调用模式
- 冲突处理：保留命名空间冲突直接跳过，非保留命名空间冲突自动添加插件名前缀

### 3. 环境分组模式

- 多插件可共享同一 Python 虚拟环境
- 按版本和依赖兼容性自动分组
- 节省资源，加快启动速度

### 4. 参数注入模式

- HandlerDispatcher 支持类型注解注入
- 优先级：类型注解 > 参数名 > legacy_args
- 支持可选类型 `Optional[Type]`

### 5. 取消传播模式

- CancelToken 统一取消机制
- 跨进程取消通过 CancelMessage
- 早到取消避免竞态条件

### 6. 插件隔离模式

- `PluginWorkerRuntime` 运行单个插件
- `GroupWorkerRuntime` 可在同一 Worker 中承载多个兼容插件
- 单个 Worker 崩溃只影响该 Worker 承载的插件集合

### 7. 热重载模式

- `dev --watch` 支持文件变更检测
- 按插件目录清理 `sys.modules` 缓存
- 确保代码变更后正确重载

---

## 文档与资源

### 完整文档目录

SDK 文档按学习路径组织，位于项目根目录的 `docs/` 文件夹：

| 级别 | 文档 | 内容 |
|------|------|------|
| **初级** | README.md | 快速开始、核心概念 |
| | 01_context_api.md | Context API 完整参考 |
| | 02_event_and_components.md | MessageEvent 和消息组件 |
| | 03_decorators.md | 装饰器详细说明 |
| | 04_star_lifecycle.md | 插件基类和生命周期 |
| | 05_clients.md | 客户端 API 文档 |
| **中级** | 06_error_handling.md | 错误处理与调试 |
| | 07_advanced_topics.md | 并发、性能优化、安全 |
| | 08_testing_guide.md | 测试指南 |
| **高级** | 09_api_reference.md | 完整 API 索引 |
| | 10_migration_guide.md | 迁移指南 |
| | 11_security_checklist.md | 安全检查清单 |
| | 12_plugin_capability_registration_flow.md | 插件能力注册流程 |
| | PROJECT_ARCHITECTURE.md | 架构设计文档 |

### 关键文件速查

> 所有源码位于 `src/astrbot_sdk/`，下表使用相对于该目录的路径。

| 文件 | 核心类/函数 | 说明 |
|------|------------|------|
| `__init__.py` | `Star`, `Context`, `MessageEvent` | 顶层入口 |
| `star.py` | `Star` | astrbot-sdk 原生插件基类 |
| `context.py` | `Context` | 运行时上下文 |
| `decorators.py` | 所有装饰器 | astrbot-sdk 装饰器定义 |
| `filters.py` | `PlatformFilter`, `MessageTypeFilter` | 过滤器定义 |
| `errors.py` | `AstrBotError` | 统一错误模型 |
| `events.py` | `MessageEvent` | 事件模型 |
| `message/components.py` | `Plain`, `Image`, `At` | 消息组件 |
| `message_components.py` | 兼容性导出 | 向后兼容（建议使用 message/components.py）|
| `runtime/peer.py` | `Peer` | 协议对等端 |
| `runtime/transport.py` | `Transport`, `StdioTransport` | 传输层抽象 |
| `runtime/capability_router.py` | `CapabilityRouter` | Capability 路由 |
| `runtime/handler_dispatcher.py` | `HandlerDispatcher` | Handler 分发 |
| `runtime/supervisor.py` | `SupervisorRuntime`, `WorkerSession` | Supervisor 运行时 |
| `runtime/worker.py` | `PluginWorkerRuntime`, `GroupWorkerRuntime` | Worker 运行时 |
| `clients/llm.py` | `LLMClient` | LLM 客户端 |

### 版本信息

- **SDK 架构版本**: astrbot-sdk
- **协议版本**: 1.0
- **Python 要求**: >=3.12

---

> 本文档描述 AstrBot SDK (astrbot-sdk) 的架构设计，完整 API 请查阅 `docs/` 目录及 `src/astrbot_sdk/` 源码。
