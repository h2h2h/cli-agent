# feat(runtime): bound pending Execution admission / 限制 pending Execution 准入

**Status / 状态：** pass

## English

### Background

The per-Session Scheduler needs a finite pending capacity controlled by the
embedding Host. The accepted design defaults that capacity to 32 and requires
immediate `queue_full` failure when a new allowed Decision would exceed it.
Policy denial remains earlier than admission and must not be obscured by a full
queue.

This issue adds the Host configuration boundary and proves its exact capacity
semantics. It does not introduce a global Runtime queue or count running lane
occupants as pending work.

### Changes

- Add a Host-facing pending Execution capacity option to
  `AgentRuntime.open`, defaulting to 32, and pass its validated snapshot into
  every Environment Session created by that Runtime.
- Reject booleans, non-integers, and values below one before opening Runtime
  resources; do not read the capacity from Workspace or Agent-authored state.
- Apply the bound only to admitted Executions waiting for a lane; a running
  Shell Execution does not consume one of the pending slots.
- Fail immediately with the existing model-visible `queue_full` error when an
  allowed Decision cannot start and the pending queue is full.
- Allocate no `exec_id`, Execution Record, completion task, or Driver resource
  for a `queue_full` request.
- Continue parsing and deciding before Scheduler admission so a denied request
  returns `policy_denied` even while the pending queue is full, and consumes no
  pending or running capacity.
- Preserve the exact allowed `ExecutionDecision`; the Scheduler may assign
  lifecycle metadata but must not rewrite or re-authorize its parse result.
- Add tests for the default and configured capacities, immediate full-queue
  failure, pending-capacity release after promotion, policy denial at
  saturation, invalid Host configuration, and fresh capacity state in a newly
  created Session.

## 中文

### 背景

每个 Session 的 Scheduler 都需要一个由嵌入式 Host 控制的有限 pending 容量。
已接受的设计将默认容量设为 32，并要求当新的 allowed Decision 会超过该容量
时，立即返回 `queue_full`。策略拒绝仍位于准入之前，不能因为队列已满而被
遮蔽。

本 issue 添加 Host 配置边界，并验证精确的容量语义。它不引入 Runtime 全局
队列，也不把占用运行 lane 的 Execution 计入 pending work。

### 变更

- 在 `AgentRuntime.open` 添加面向 Host 的 pending Execution 容量选项，默认
  为 32，并将验证后的快照传入该 Runtime 创建的每个 Environment Session。
- 在打开 Runtime 资源前拒绝布尔值、非整数和小于一的值；不从 Workspace 或
  Agent 编写的状态中读取容量。
- 只对已经准入且正在等待 lane 的 Execution 应用该上限；正在运行的 Shell
  Execution 不占用 pending slot。
- 当 allowed Decision 无法立即启动且 pending 队列已满时，立即返回现有模型
  可见的 `queue_full` 错误。
- 对 `queue_full` 请求不分配 `exec_id`、Execution Record、completion task 或
  Driver 资源。
- 继续在 Scheduler 准入前完成解析和决策，因此即使 pending 队列已满，被拒绝
  的请求仍返回 `policy_denied`，且不占用 pending 或 running 容量。
- 保留原始 allowed `ExecutionDecision`；Scheduler 可以分配生命周期 metadata，
  但不能改写或重新授权其 parse result。
- 添加测试，覆盖默认与自定义容量、队满立即失败、提升后释放 pending 容量、
  饱和状态下的策略拒绝、无效 Host 配置，以及新 Session 的全新容量状态。
