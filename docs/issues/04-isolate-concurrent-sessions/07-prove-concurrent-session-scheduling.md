# test(runtime): prove concurrent Session scheduling / 验证并发 Session 调度

**Status / 状态：** pass

## English

### Background

Focused Scheduler and Environment Binding tests are necessary for race and
capacity coverage, but milestone 04 is complete only when the public
`AgentRuntime` path proves that two Agent Sessions can make progress
concurrently while retaining independent conversation and execution state.

This final tracer bullet should validate the parent issue without expanding the
model-visible surface. AgentLoop still dispatches Tool Calls from one Assistant
Message in order; concurrent Tool-lane batch dispatch remains assigned to
milestone 10.

### Changes

- Add a deterministic offline integration scenario that opens one
  `AgentRuntime` and concurrently runs scripted turns in two Session IDs.
- Have each Session start synchronized Shell work and prove that both
  Executions overlap, while an additional Shell Execution in either Session
  remains FIFO behind that Session's running work.
- Exercise a small configured pending capacity and assert model-visible queued
  Snapshots and `queue_full` errors through the public Runtime path.
- Include a denied `exec` while capacity is saturated and prove it returns
  `policy_denied` without displacing or delaying admitted work.
- Assert each Session's Model Requests retain only its own System Message,
  Conversation History, Tool Calls, and ordered Tool Results.
- Assert foreign Handles remain indistinguishable from nonexistent Handles
  through `output` and `kill`.
- Close one Session while it owns running and queued work, prove the peer
  Session completes unaffected, then reuse the closed ID and prove fresh
  conversation and execution state.
- Close the Runtime and assert no child process, Scheduler task, pending
  Execution, or Environment Binding remains active.
- Keep the built-in model-visible surface exactly `exec`, `output`, and `kill`;
  do not add a public Scheduler, Driver schema, Tool lane, persistent Session
  state, network dependency, or live Provider credential.

## 中文

### 背景

聚焦的 Scheduler 和 Environment Binding 测试对于覆盖竞态与容量是必要的，但
只有公共 `AgentRuntime` 路径证明两个 Agent Session 可以并发推进，同时保持
conversation 和 execution 状态相互独立，milestone 04 才算完成。

最后一个 tracer bullet 应验证父 issue，而不扩展模型可见界面。AgentLoop 仍按
顺序派发同一个 Assistant Message 中的 Tool Call；Tool lane 的并发 batch
dispatch 继续留给 milestone 10。

### 变更

- 添加确定性的离线集成场景：打开一个 `AgentRuntime`，并发运行两个 Session ID
  中的 scripted turn。
- 让两个 Session 分别启动经过同步的 Shell 工作，证明两者确实重叠；同时让
  任一 Session 中额外的 Shell Execution 按 FIFO 等待该 Session 的 running
  工作。
- 配置一个较小的 pending 容量，并通过公共 Runtime 路径断言模型可见的 queued
  Snapshot 和 `queue_full` 错误。
- 在容量饱和时加入被拒绝的 `exec`，证明它返回 `policy_denied`，且不替换或
  延迟已经准入的工作。
- 断言每个 Session 的 Model Request 只保留自己的 System Message、
  Conversation History、Tool Call 和有序 Tool Result。
- 断言 foreign Handle 通过 `output` 和 `kill` 仍与不存在的 Handle 无法区分。
- 在一个 Session 同时拥有 running 与 queued 工作时关闭它，证明 peer Session
  不受影响并正常完成；随后复用已关闭 ID，证明 conversation 和 execution 状态
  都是全新的。
- 关闭 Runtime，并断言没有子进程、Scheduler task、pending Execution 或
  Environment Binding 仍处于活动状态。
- 保持模型可见 built-in surface 严格为 `exec`、`output` 和 `kill`；不添加公共
  Scheduler、Driver schema、Tool lane、持久化 Session 状态、网络依赖或真实
  Provider 凭据。
