# feat(runtime): close and recreate scheduled Sessions / 关闭并重建带调度状态的 Session

**Status / 状态：** pass

## English

### Background

Session close currently terminates every known running Execution and clears its
records. With a Scheduler, close must also prevent queued work from being
promoted, wake waiters, release lane bookkeeping, and remain isolated from
other Sessions. Reusing the same host-facing Session ID must create a fresh
Agent Loop and Environment Session rather than reconnecting to the closed
Scheduler.

Persistent Session cwd and environment mutation do not exist yet. Freshness in
this milestone therefore means no old conversation, Scheduler, Handle, output,
Cursor, or transient execution state is reused; filtered environment snapshots
remain milestone 05 work.

### Changes

- Mark an Environment Session as closing before cleanup so no new admission or
  queued-to-running promotion can begin after close starts.
- Cancel every queued Execution without starting its Driver and clean up every
  running Execution through the shared Driver lifecycle.
- Wake pending `exec`, `output`, and cancellation waiters with stable terminal
  or closed-session behavior and avoid leaving background Scheduler tasks.
- Release pending entries, lane occupancy, Execution Records, output buffers,
  and Cursors when the Environment Session is removed.
- Keep Session close idempotent and ensure closing one Session does not cancel,
  drain, or reorder work owned by another Session.
- Preserve Runtime close as Session-by-Session cleanup followed by
  Workspace-scoped Environment Kernel cleanup, without host-facing Driver-type
  branches.
- Make later reuse of the same host-facing Session ID create a fresh
  Conversation History, System Message snapshot, Environment Binding,
  Scheduler, submission sequence, and Handle namespace.
- Do not restore old Executions or claim persistent `cd`/`export` behavior; the
  current Workspace cwd behavior remains unchanged.
- Add race-focused tests for closing with both running and queued work, Runtime
  close across multiple Sessions, idempotent close, unaffected peer Sessions,
  and fresh same-ID recreation.

## 中文

### 背景

当前 Session close 会终止所有已知 running Execution 并清空记录。引入 Scheduler
后，close 还必须阻止 queued 工作被提升、唤醒 waiter、释放 lane bookkeeping，
并且不影响其他 Session。之后复用同一个 Host 可见 Session ID 时，必须创建全新
的 Agent Loop 和 Environment Session，而不是重新连接已经关闭的 Scheduler。

目前还没有持久化的 Session cwd 和环境变量修改。因而本 milestone 中的“全新”
是指不复用旧 conversation、Scheduler、Handle、输出、Cursor 或瞬态执行状态；
过滤后的环境快照仍属于 milestone 05。

### 变更

- 在清理前将 Environment Session 标记为 closing，确保 close 开始后不能再进行
  新准入或 queued-to-running 提升。
- 不启动 Driver 就取消所有 queued Execution，并通过共享 Driver 生命周期清理
  所有 running Execution。
- 唤醒 pending 的 `exec`、`output` 和 cancellation waiter，返回稳定的终止或
  Session 已关闭行为，且不遗留后台 Scheduler task。
- 移除 Environment Session 时释放 pending item、lane 占用、Execution Record、
  output buffer 和 Cursor。
- 保持 Session close 幂等，并确保关闭一个 Session 不会取消、清空或重排另一个
  Session 的工作。
- Runtime close 继续先逐 Session 清理，再清理 Workspace 范围的 Environment
  Kernel；Host-facing Runtime 不按 Driver 类型分支。
- 后续复用相同 Host 可见 Session ID 时，创建全新的 Conversation History、
  System Message snapshot、Environment Binding、Scheduler、提交序号和 Handle
  namespace。
- 不恢复旧 Execution，也不宣称 `cd`/`export` 会持久化；当前 Workspace cwd
  行为保持不变。
- 添加面向竞态的测试，覆盖同时存在 running 与 queued 工作时关闭、多个 Session
  下关闭 Runtime、幂等 close、不受影响的 peer Session，以及相同 ID 的全新重建。
