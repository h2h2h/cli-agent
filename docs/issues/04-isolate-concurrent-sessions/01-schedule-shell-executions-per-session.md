# feat(environment): schedule Shell Executions per Session / 按 Session 调度 Shell Execution

**Status / 状态：** pass

## English

### Background

Every allowed `ExecutionDecision` currently becomes a running
`_ExecutionRecord` immediately and starts its Shell process in an independent
task. Concurrent `exec` dispatches through one Environment Binding can
therefore run multiple Shell Executions at once, and there is no pending state,
stable submission sequence, or lane capacity.

Milestone 04 needs the first private, driver-aware Scheduler slice. It remains
Shell-only, but its state and selection rules must leave room for a later Tool
lane without turning the implementation into one queue-head-only global FIFO.

### Changes

- Add one private Execution Scheduler owned by each Environment Session.
- Admit only allowed, immutable Decisions and assign each accepted Execution a
  stable, monotonically increasing submission sequence.
- Derive the scheduling lane from the Runtime-trusted operation selected in the
  `CommandParseResult`; do not accept an Agent-supplied lane or concurrency
  hint.
- Implement the Shell lane with running capacity one and FIFO start order
  within that lane.
- Represent queued Executions with the existing `queued` status and return a
  queued Snapshot when `exec`'s `wait_ms` expires before the Execution starts.
- Promote the next runnable Shell Execution when the current one reaches a
  terminal state, including process-start failure, without repeating parsing
  or policy evaluation.
- Keep running Executions outside the pending-count calculation and preserve
  existing output, Cursor, wait, truncation, and terminal Snapshot semantics.
- Structure runnable selection by lane availability so a future runnable Tool
  item can bypass an earlier item blocked only on the Shell lane; do not add a
  Tool Driver or Tool concurrency in this issue.
- Add deterministic Environment Binding tests proving at-most-one running
  Shell Execution, queued Snapshots, FIFO start order, and promotion after
  success and failure.

## 中文

### 背景

当前每个允许的 `ExecutionDecision` 都会立刻变成状态为 `running` 的
`_ExecutionRecord`，并在独立 task 中启动 Shell 进程。因此，通过同一个
Environment Binding 并发派发多个 `exec` 时，多个 Shell Execution 可能同时
运行；系统也没有 pending 状态、稳定的提交序号或 lane 容量。

Milestone 04 需要落地第一个私有、driver-aware 的 Scheduler 切片。该阶段仍然
只有 Shell，但状态和选择规则必须为未来 Tool lane 留出空间，不能把实现固化为
只能检查全局队首的 FIFO。

### 变更

- 为每个 Environment Session 添加一个私有 Execution Scheduler。
- 只准入允许且不可变的 Decision，并为每个被接受的 Execution 分配稳定、单调
  递增的提交序号。
- 根据 `CommandParseResult` 中由 Runtime 信任的 operation 推导 scheduling
  lane；不接受 Agent 提供的 lane 或并发提示。
- 实现运行容量为一的 Shell lane，并保证该 lane 内按 FIFO 顺序启动。
- 使用现有 `queued` 状态表示排队中的 Execution；如果 Execution 启动前
  `exec` 的 `wait_ms` 已耗尽，则返回 queued Snapshot。
- 当前 Execution 进入终止状态后提升下一个可运行的 Shell Execution，包括
  进程启动失败的情况；提升过程不重复解析或策略判断。
- pending 数量不包含正在运行的 Execution，并保留现有 output、Cursor、wait、
  truncation 和终止 Snapshot 语义。
- 按 lane 可用性组织 runnable 选择，使未来可运行的 Tool item 可以绕过仅因
  Shell lane 忙碌而等待的更早 item；本 issue 不添加 Tool Driver 或 Tool
  并发。
- 添加确定性的 Environment Binding 测试，验证同一时刻最多运行一个 Shell
  Execution、queued Snapshot、FIFO 启动顺序，以及成功和失败后的提升。
