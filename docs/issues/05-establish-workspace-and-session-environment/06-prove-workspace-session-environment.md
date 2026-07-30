# test(runtime): prove Workspace and Session environment semantics / 验证 Workspace 与 Session 环境语义

**Status / 状态：** pass

## English

### Background

Focused loader, parser, Session, and Driver tests are necessary but the
milestone is complete only when the public Runtime path proves the combined
Workspace-open, Session-isolation, export-ordering, and child-inheritance
contract.

### Changes

- Add a deterministic public `AgentRuntime` scenario that opens a Workspace
  with configured custom values and creates two Sessions.
- Prove both Sessions start from the same Runtime-open Workspace values while
  later exports remain private.
- Prove top-level export is visible to later FIFO Shell work, survives later
  turns in the same Session, and disappears after close/recreate.
- Prove Host variables are inherited, Session values override collisions, and
  later Host changes affect later Executions.
- Prove edits under `.workspace/env` do not affect the active Runtime but do
  affect a later open.
- Exercise Runtime close, queued export cancellation, malformed Workspace
  configuration, and `.workspace` path conflicts without leaked tasks or
  partially initialized Sessions.
- Assert the model-visible surface remains exactly `exec`, `output`, and
  `kill`.
- Run the full offline test, lint, format, and whitespace gates and update
  `docs/handoff.md` with the completed behavior and next milestone.

## 中文

### 背景

聚焦 loader、parser、Session 和 Driver 测试是必要的，但只有公共 Runtime
路径验证组合后的 Workspace-open、Session 隔离、export 顺序和 child 继承
契约，milestone 才完成。

### 变更

- 添加确定性的公共 `AgentRuntime` 场景：打开含自定义配置的 Workspace 并创建
  两个 Session。
- 证明两个 Session 从相同 Runtime-open Workspace 值开始，之后 export 相互
  隔离。
- 证明顶层 export 对后续 FIFO Shell 工作可见、在同一 Session 后续 turn 中
  保留，并在 close/recreate 后消失。
- 证明 Host 变量被继承、Session 值覆盖冲突，之后的 Host 修改影响之后的
  Execution。
- 证明 `.workspace/env` 修改不影响活动 Runtime，但影响之后的 open。
- 覆盖 Runtime close、queued export cancellation、非法 Workspace 配置和
  `.workspace` path conflict，确保没有泄漏 task 或部分初始化 Session。
- 断言模型可见 surface 仍严格为 `exec`、`output`、`kill`。
- 运行完整 offline test、lint、format、whitespace gate，并更新
  `docs/handoff.md` 记录完成行为和下一 milestone。
