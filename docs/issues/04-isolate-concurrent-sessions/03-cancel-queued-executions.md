# feat(environment): cancel queued Executions / 取消 queued Execution

**Status / 状态：** pass

## English

### Background

Once Shell admission can return a queued Handle, `kill` must terminate that
Execution without waiting for it to acquire the Shell lane. The current
cancellation path waits for `process_ready`, which is valid only for work that
has already started and would deadlock for a queued Execution that never owns a
process.

Queued cancellation is Scheduler lifecycle work. It must not invoke the Shell
Driver, repeat Execution Policy, or expose a new model-visible operation.

### Changes

- Make `kill` locate queued and running Execution Records through the same
  Session-private Handle lookup.
- Atomically remove a queued Execution from pending selection, mark it
  `killed`, and wake any `exec` or `output` waiter without starting a process or
  allocating another Driver resource.
- Return the normal terminal Snapshot for the killed queued Execution, with
  stable Cursor and empty-or-retained output semantics.
- Release the pending slot immediately so a later allowed Decision can be
  admitted.
- Preserve idempotent `kill` behavior after the queued Execution is terminal.
- Resolve promotion-versus-kill races so one Execution is either claimed once
  by its lane or cancelled before start, never both.
- Keep running Shell cancellation and graceful-then-forced process-group
  cleanup on the existing shared Execution lifecycle.
- Add deterministic tests for killing the first, middle, and last pending
  Execution, capacity reuse, idempotent repeated kill, waiter notification,
  and the promotion race boundary.

## 中文

### 背景

当 Shell 准入能够返回 queued Handle 后，`kill` 必须在该 Execution 获得 Shell
lane 之前终止它。当前取消路径会等待 `process_ready`；这只适用于已经开始的
工作，对永远不会拥有进程的 queued Execution 会造成死锁。

queued 取消属于 Scheduler 生命周期工作。它不能调用 Shell Driver、重复
Execution Policy，也不能新增模型可见操作。

### 变更

- 让 `kill` 通过同一个 Session-private Handle lookup 查找 queued 和 running
  Execution Record。
- 原子地从 pending 选择中移除 queued Execution，将其标记为 `killed`，并唤醒
  所有 `exec` 或 `output` waiter；不启动进程，也不分配其他 Driver 资源。
- 为被取消的 queued Execution 返回普通终止 Snapshot，并保持稳定 Cursor 和
  空输出或已保留输出语义。
- 立即释放 pending slot，使后续 allowed Decision 可以准入。
- 对已经终止的 queued Execution 保持幂等 `kill` 行为。
- 处理提升与取消的竞态，确保一个 Execution 要么只被 lane claim 一次，要么
  在启动前被取消，绝不会同时发生。
- running Shell 的取消以及先优雅后强制的进程组清理继续走现有共享 Execution
  生命周期。
- 添加确定性测试，覆盖取消 pending 队列首部、中部和尾部、容量复用、重复 kill
  幂等、waiter 唤醒，以及提升竞态边界。
