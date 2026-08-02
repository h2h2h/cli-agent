# refactor(runtime): replace execution lanes with global barriers

**状态：** pending

## 背景

当前 Scheduler 通过 `_ExecutionLane.DEFAULT` 和 `_ExecutionLane.TOOL` 分别维护
Shell/Custom 与 Tool 的运行容量。该设计让 Driver kind 影响并发策略，并允许 Tool
越过正在运行的 Shell。统一 Command 模型不再需要按命令类型划分 lane。

## 影响

完成后，同一 Session 使用一个 pending queue 和一个 `parallel_limit`。Scheduler
只关心 route 是否 `parallel_safe`，不会知道命令是 Shell、Custom 还是 Tools。
并行命令仍然可以批量执行，但 serial command 会形成全局 barrier，后续命令不能
越过它。

## 变更

- 删除 `_ExecutionLane`、`route.lane` 和所有 lane-specific 状态。
- 删除 Scheduler 的 `_claim_lane()` 和 `_lane_limit()`。
- 删除 `tool_parallel_limit`，保留一个 `parallel_limit`。
- 将 admission 改为有序 serial barrier：
  - queue head 为 parallel-safe 时，连续 parallel-safe command 可以批量 claim；
  - 批次受 `parallel_limit` 限制；
  - serial command 必须等待更早的运行项完成，并独占运行阶段；
  - serial barrier 后的 command 不能 overtaking。
- 保留现有 `queue_limit`、queued kill、running kill、queue-full 和 close 语义。
- 保留跨 Session 并发，不允许同一 Session 的 Tool 工作使用独立 lane。
- 更新 Execution snapshot、测试辅助函数和白盒测试中对 lane 的断言。
- 增加混合 Shell、Custom、Tool 的顺序测试，证明：
  - parallel-safe commands 使用统一容量；
  - serial command 是全局 barrier；
  - 完成顺序可以不同，但 admission 和 Tool Result 顺序保持既有契约。
