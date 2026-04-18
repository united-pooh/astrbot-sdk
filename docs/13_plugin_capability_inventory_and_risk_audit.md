# 插件作者能力盘点与多插件风险审计

最后更新：2026-04-18

## 目的

这份文档只审计一件事：`astrbot-sdk` 暴露给插件作者的能力里，哪些能力会在多插件并存时放大冲突、状态漂移或宿主级副作用。

本次审计的结论已经落地到代码：

- 删除全局 MCP 写入口
- 删除 `MessageEvent` 上对默认 LLM 主链路的请求级干预入口

## 盘点范围

面向插件作者的公开能力主要分为 8 类：

1. 插件骨架与运行时对象
   - `Star`
   - `Context`
   - `MessageEvent`
   - `ScheduleContext`
   - `ConversationSession`
2. 触发器与行为声明
   - `on_command`
   - `on_message`
   - `on_event`
   - `on_schedule`
   - `conversation_command`
   - `background_task`
   - `validate_config`
   - `on_provider_change`
3. 过滤、权限与限流
   - `require_admin` / `admin_only`
   - `require_permission`
   - `platforms`
   - `message_types`
   - `group_only` / `private_only`
   - `priority`
   - `rate_limit`
   - `cooldown`
   - `custom_filter` / `all_of` / `any_of`
4. 跨插件或宿主集成能力
   - `provide_capability`
   - `http_api`
   - `register_llm_tool`
   - `register_agent`
   - `register_skill`
5. Context 客户端
   - `llm`
   - `memory`
   - `db`
   - `platform`
   - `permission`
   - `providers`
   - `provider_manager`
   - `metadata`
   - `http`
   - `registry`
   - `skills`
   - `session_plugins`
   - `session_services`
   - 以及 conversation/persona/kb/message_history 等管理客户端
6. 消息结果与消息组件
   - `MessageEventResult`
   - `MessageChain`
   - `MessageBuilder`
   - `Plain` / `Image` / `At` / `File` / `Record` / `Video` 等
7. LLM 与 Provider 辅助类型
   - `ProviderRequest`
   - `ProviderMeta`
   - `ProviderType`
   - TTS / STT / Embedding / Rerank 代理
8. 开发辅助
   - `PluginHarness`
   - `MockContext`
   - testing helper 与模板文档

## 风险分级

### 低风险

这些能力天然是插件内行为，或者已经由插件命名空间隔离：

- 命令、消息、定时、会话、过滤器、限流
- `ctx.llm.chat()` / `chat_raw()` / `stream_chat()`
- `ctx.memory` / `ctx.db`
- 消息组件与结果构造
- `register_skill`
- `provide_capability`
- `http_api`

### 中风险

这些能力会影响执行顺序或共享宿主资源，但已有明确边界，暂不删除：

- `event.stop_event()`
- `priority`
- `register_agent`
- `ctx.registry` 的 handler 白名单
- `on_event("llm_request")` 与 `on_event("decorating_result")` 这类宿主链路钩子

它们仍可能被滥用，但风险主要来自插件设计，而不是公开 API 本身直接暴露了全局可变状态。

### 高风险

这些能力会让单个插件写入宿主级共享状态，或在一次消息请求里直接争夺默认链路控制权。

#### 1. MessageEvent 上的默认 LLM 链路干预

删除前包含：

- `MessageEvent.request_llm()`
- `MessageEvent.should_call_llm()`
- `MessageEvent.set_result()`
- `MessageEvent.get_result()`
- `MessageEvent.clear_result()`

风险：

- 多个插件可以在同一条消息请求里争夺“是否进入默认 LLM 主链路”
- 多个插件可以抢写请求级结果，形成最后写入者覆盖
- 这会让插件间耦合到宿主内部消息处理主链路，而不是只处理自己的逻辑

处置：

- 全部删除
- 对应底层 capability 一并删除：
  - `system.event.llm.get_state`
  - `system.event.llm.request`
  - `system.event.result.get`
  - `system.event.result.set`
  - `system.event.result.clear`

## 本次保留但特别说明的能力

### `ctx.llm.*`

保留。

原因：

- 这是插件主动发起的独立 LLM 调用
- 它不会直接改写宿主“这条消息默认是否进入 LLM”的决策位
- 风险主要是插件自身滥用模型，而不是插件间状态冲突

### `ctx.tool_loop_agent()`

保留。

原因：

- 它是插件显式调用的 tool loop，而不是消息主链路的隐式仲裁开关
- 当前风险低于请求级默认 LLM 状态写入口

## 审计后能力边界

删除后，插件作者仍然可以：

- 声明命令、消息、事件、定时、会话流程
- 主动调用 LLM、Provider、Memory、DB、HTTP、Skill
- 导出 capability、HTTP API、LLM tool、Agent 元数据
- 使用 `on_event("llm_request")` 修改插件自己收到的 `ProviderRequest`

删除后，插件作者不能再：

- 通过 `MessageEvent` 直接争夺默认 LLM 主链路的开关
- 通过 `MessageEvent` 直接覆盖请求级最终结果存储槽

## 审计结论

SDK 面向插件作者的公开面里，真正会显著破坏多插件兼容性的危险能力，主要集中在：

1. 宿主级共享资源写入口
2. 请求级默认主链路仲裁入口

本次删除的是这两类能力，而不是普通的插件内功能。这样做能在不明显削弱常规插件开发能力的前提下，收紧最容易导致多插件相互踩踏的边界。
