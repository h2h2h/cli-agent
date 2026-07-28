# fix(environment): keep Execution Handles Session-private / 保持 Execution Handle 的 Session 私有性

**Status / 状态：** pass

## English

### Background

Each Environment Session already stores its own Execution Records, but the new
Scheduler adds queued Handles and more lifecycle paths that could accidentally
look up or cancel work outside the bound Session. A Handle is an opaque
Session-scoped capability, not a Workspace-wide process identifier.

The contract also intentionally makes foreign and nonexistent Handles
indistinguishable so the Runtime does not reveal whether another Session owns
an Execution.

### Changes

- Keep Scheduler state, Execution Records, pending entries, lane occupancy,
  output buffers, and Cursors owned by exactly one Environment Session.
- Resolve `output` and `kill` only against the Environment Session selected by
  the calling `EnvironmentBinding`.
- Return the same existing `unknown_execution` code and
  `execution not found` message for foreign, nonexistent, and handles invalid
  after Session close.
- Apply the same lookup semantics to queued, running, and terminal Executions.
- Do not expose Environment Session IDs, submission sequences, lane names,
  operating-system process IDs, or ownership hints in model-visible
  Snapshots or errors.
- Keep `output` and `kill` outside normal Execution admission so they consume no
  queue capacity and do not invoke policy.
- Add two-binding tests covering foreign queued, running, and terminal Handles
  for both `output` and `kill`, and prove that rejected foreign cancellation
  does not change the owning Session's Execution.

## 中文

### 背景

每个 Environment Session 已经分别保存自己的 Execution Record，但新的
Scheduler 会增加 queued Handle 和更多生命周期路径，可能意外查询或取消绑定
Session 之外的工作。Handle 是不透明、Session 范围的 capability，不是
Workspace 范围的进程标识符。

该契约还刻意让 foreign Handle 与不存在的 Handle 无法区分，避免 Runtime
泄露另一个 Session 是否拥有某个 Execution。

### 变更

- 让 Scheduler 状态、Execution Record、pending item、lane 占用、输出 buffer
  和 Cursor 都严格归属于一个 Environment Session。
- `output` 和 `kill` 只能在调用方 `EnvironmentBinding` 选定的 Environment
  Session 内解析 Handle。
- 对 foreign、不存在以及 Session 关闭后失效的 Handle，统一返回现有
  `unknown_execution` code 和 `execution not found` message。
- 对 queued、running 和 terminal Execution 使用相同的 lookup 语义。
- 不在模型可见 Snapshot 或错误中暴露 Environment Session ID、提交序号、lane
  名称、操作系统进程 ID 或所有权提示。
- `output` 和 `kill` 继续位于普通 Execution 准入之外，因此不消耗队列容量，也
  不调用策略。
- 添加双 binding 测试，覆盖 foreign queued、running 和 terminal Handle 的
  `output` 与 `kill`，并证明被拒绝的 foreign cancellation 不会改变所有者
  Session 中的 Execution。
