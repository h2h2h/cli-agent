# feat(environment): run Shell work concurrently across Sessions / 跨 Session 并发运行 Shell 工作

**Status / 状态：** pass

## English

### Background

The Shell lane capacity is per Session, not per Workspace or Runtime. A single
Runtime-wide semaphore would satisfy same-Session serialization but would
incorrectly make unrelated Sessions block one another.

This issue proves the positive concurrency side of the design while retaining
normal shared-Workspace filesystem behavior. It does not add Workspace-wide
transactions or claim that concurrent shell writes are conflict-safe.

### Changes

- Ensure each Environment Session owns independent pending capacity, submission
  sequence, Shell lane occupancy, and promotion work.
- Allow Shell Executions in two Sessions bound to the same Environment Kernel
  to overlap in wall-clock execution.
- Preserve at-most-one-running Shell Execution independently inside each of
  those Sessions.
- Ensure a full queue or busy Shell lane in one Session does not delay
  admission or execution in another Session.
- Keep the Execution Supervisor's output, cancellation, and Driver cleanup
  behavior backend-neutral and scoped by the owning Session.
- Preserve immediate shared visibility of ordinary Workspace filesystem
  effects without adding serialization, optimistic versions, or a filesystem
  safety guarantee to arbitrary Shell commands.
- Add synchronization-based tests using events or marker files rather than
  timing-only assertions to prove overlap, per-Session serialization, and lack
  of cross-Session head-of-line blocking.

## 中文

### 背景

Shell lane 的容量属于每个 Session，而不是每个 Workspace 或 Runtime。一个
Runtime 全局 semaphore 虽然也能让同一 Session 串行，却会错误地让互不相关的
Session 相互阻塞。

本 issue 验证该设计的正向并发能力，同时保留普通的共享 Workspace 文件系统
行为。它不添加 Workspace 全局事务，也不宣称并发 shell 写入具备冲突安全性。

### 变更

- 确保每个 Environment Session 分别持有 pending 容量、提交序号、Shell lane
  占用和提升工作。
- 允许绑定到同一个 Environment Kernel 的两个 Session 中的 Shell Execution
  在实际运行时间上重叠。
- 在这两个 Session 内部分别保持同一时刻最多运行一个 Shell Execution。
- 确保一个 Session 的队列已满或 Shell lane 忙碌不会延迟另一个 Session 的
  准入或执行。
- 保持 Execution Supervisor 的输出、取消和 Driver 清理行为后端中立，并按
  所有者 Session 划定范围。
- 保留普通 Workspace 文件系统效果的即时共享可见性，但不为任意 Shell 命令
  添加串行化、乐观版本或文件系统安全保证。
- 使用 event 或 marker file 等同步机制添加测试，而不是只依赖时间断言，以
  验证重叠执行、Session 内串行以及不存在跨 Session 队首阻塞。
