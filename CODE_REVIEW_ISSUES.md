# SDK Cleanup Review

## Summary
目标是把 SDK 收敛成“未发布前就干净”的状态。基于当前源码、文档、`vendor/` 快照和测试，我把结论分成三类：

- 可以立即删除：本地生成物和缓存
- 可以在首发前删除：未发布的迁移/兼容 shim
- 现在不能删：vendored testing helper 契约和 runtime 根导出核心原语

当前真正的问题不是“还有很多明显死代码没删”，而是“已经开始删兼容层，但源码、文档、vendor 还没一起收口”。

## 可立即删除
这些内容是纯生成物，不属于 SDK 源码，删掉没有行为风险：

- `.pytest_cache/`
- `.ruff_cache/`
- `.coverage`
- `tmp/`
- `src/astrbot_sdk/**/__pycache__/`
- `src/astrbot_sdk/**/*.pyc`

证据：

- `.gitignore` 已明确忽略这些路径
- `git ls-files "*.pyc"` 没有返回结果，说明它们不是仓库契约的一部分

## 可以删，但要一次性删干净
如果确认 SDK 还没正式发布、外部插件没有依赖旧路径，那么下面这些 shim 可以删：

- `src/astrbot_sdk/message_components.py`
- `src/astrbot_sdk/message_result.py`
- `src/astrbot_sdk/message_session.py`
- `src/astrbot_sdk/_command_model.py`
- `src/astrbot_sdk/_plugin_logger.py`
- `src/astrbot_sdk/_star_runtime.py`

删除这批文件在“未发布前”是合理的，因为它们只是转发到 canonical 实现，本身不承载业务逻辑。

但当前删除还不完整，留下了两个高优先级问题。

### High: 文档仍在引用已删除路径
以下文档还在写旧模块名，会把未来使用者继续带到不存在的路径上：

- `docs/api/message_components.md`
- `docs/api/message_result.md`
- `docs/api/message_event.md`
- `docs/api/types.md`
- `docs/api/utils.md`

这说明当前不是“完成清理”，而是“源码删了，但文档契约没删完”。

### High: `vendor/` 快照与源码决策不一致
`vendor/VENDORED.md` 明确写了 `vendor/src/astrbot_sdk/` 应从 `src/astrbot_sdk/` 同步生成，但 vendored snapshot 里仍保留了整组已在源码删除的 shim，例如：

- `vendor/src/astrbot_sdk/_command_model.py`
- `vendor/src/astrbot_sdk/_plugin_logger.py`
- `vendor/src/astrbot_sdk/_star_runtime.py`
- `vendor/src/astrbot_sdk/message_components.py`
- `vendor/src/astrbot_sdk/message_result.py`
- `vendor/src/astrbot_sdk/message_session.py`

这会让“源码想收窄 API”与“subtree 消费端仍继续暴露旧入口”并存，后续很容易出现：

- SDK 仓库测试通过，但主仓库 subtree 行为与源码不一致
- 后续有人误以为这些 shim 仍是正式支持面

结论：如果要删，就要把 `src/`、`docs/`、`vendor/` 一起删并重新生成 snapshot。

## 不建议删除
下面这些表面上看像重复层，但按当前仓库契约不该删：

- `src/astrbot_sdk/testing.py`
- `src/astrbot_sdk/_testing_support.py`
- `src/astrbot_sdk/_internal/testing_support.py`

原因：

- `vendor/VENDORED.md`
- `AGENTS.md`
- `CLAUDE.md`

这三处都明确说明它们是 AstrBot subtree 消费端仍依赖的最小 testing helper 契约。删它们不是清理，是破坏下游依赖。

同理，`astrbot_sdk.runtime` 根导出目前也不该继续收窄到比现在更小。`AGENTS.md` / `CLAUDE.md` 已明确允许保留：

- `Peer`
- `Transport`
- `CapabilityRouter`
- `HandlerDispatcher`

## 次级问题
这些不是“删文件”优先项，但会持续拉低 SDK 的整洁度：

- `src/astrbot_sdk/_internal/command_model.py` 里还有 `# TODO:文档内容喵`
- `src/astrbot_sdk/runtime/transport.py`、`src/astrbot_sdk/_internal/decorator_lifecycle.py` 里还有未收口 TODO
- 文档存在重复维护面：`docs/README.md`、`docs/api/*.md`、`docs/PROJECT_ARCHITECTURE.md` 同时承担 API 指南和迁移说明，导致这次路径变更出现“部分更新、部分遗留”

这些不一定该删除文件，但应该列入“首发前收口清单”。

## 建议执行顺序
1. 删除本地生成物：缓存、`tmp/`、`__pycache__`、`.pyc`
2. 确认旧 shim 不再保留，然后把 `src/` 中这 6 个 shim 删除保持不回退
3. 全量清理文档里的旧路径引用
4. 重新生成或同步 `vendor/` 快照，让它与 `src/` 决策一致
5. 保留 `testing.py` / `_testing_support.py` / `_internal/testing_support.py`
6. 最后清 TODO 和重复文档入口

## 验证
我跑过的验证命令：

```bash
python -m pytest tests/test_sync_vendor_script.py tests/test_request_id_overlay_mapping.py -q
python -c "import sys; sys.path.insert(0, 'src'); import astrbot_sdk; import astrbot_sdk.testing; import astrbot_sdk.clients; import astrbot_sdk.llm; import astrbot_sdk.protocol; print('imports-ok')"
```

结果：

- `5 passed`
- `imports-ok`
