# feat(runtime): manage idempotent Agent Sessions / 管理幂等的 Agent Session

**Status / 状态：** pass

## English

### Background

Issue 01 requires a host to get or create a Session, run a turn, close that Session, and close the Runtime. A Session is a Runtime-owned record joining one Agent Loop, one fixed Model Provider, and one Environment Binding; it is not a separately constructed public module.

Only the lifecycle needed by the deterministic single-Session scenario belongs here. Concurrent Sessions, bounded execution queues, environment isolation, and provider retry policy remain later work.

### Changes

- Add an internal Session record owned by `AgentRuntime`.
- Implement get-or-create behavior for an active `session_id` without resetting Conversation History or replacing its Model Provider.
- Bind one Agent Loop and one Environment Binding when a Session is first created.
- Allow a Session to use the Runtime default Provider or an explicitly supplied Provider at creation time.
- Expose `run_turn(session_id, message)` through `AgentRuntime` and stream provider-neutral events to the host.
- Implement idempotent `close_session(session_id)` and release the Session's environment and conversation state.
- Make reuse of a closed ID create fresh Session state.
- Close every active Session before closing Runtime-owned resources.
- Add tests covering get-or-create identity, preserved history, fresh reuse after close, unknown/closed Session behavior, and Runtime-wide cleanup.

## 中文

### 背景

Issue 01 要求宿主能够获取或创建 Session、运行一个 turn、关闭该 Session，并关闭 Runtime。Session 是 Runtime 持有的内部记录，将一个 Agent Loop、一个固定 Model Provider 和一个 Environment Binding 关联起来；它不是可单独构造的公共模块。

这里只有确定性单 Session 场景所需的生命周期。并发 Session、有界 Execution 队列、环境隔离和 Provider 重试策略仍属于后续工作。

### 变更

- 添加由 `AgentRuntime` 持有的内部 Session 记录。
- 为活动 `session_id` 实现 get-or-create 行为，不重置 Conversation History，也不替换其 Model Provider。
- Session 首次创建时绑定一个 Agent Loop 和一个 Environment Binding。
- 允许 Session 在创建时使用 Runtime 默认 Provider，或使用显式提供的 Provider。
- 通过 `AgentRuntime` 暴露 `run_turn(session_id, message)`，并向宿主流式产出供应商中立事件。
- 实现幂等的 `close_session(session_id)`，释放 Session 的环境和对话状态。
- 关闭后复用相同 ID 时创建全新的 Session 状态。
- 关闭 Runtime 自有资源之前，先关闭所有活动 Session。
- 添加测试，覆盖 get-or-create 身份、历史保留、关闭后全新复用、未知或已关闭 Session 行为以及 Runtime 全局清理。
