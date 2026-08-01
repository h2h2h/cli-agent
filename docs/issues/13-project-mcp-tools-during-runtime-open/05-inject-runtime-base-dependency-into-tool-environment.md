# feat(tools): inject the Runtime base dependency into the Tool Environment / 向 Tool Environment 注入 Runtime 基础依赖

**Status / 状态：** pending

## English

### Background

M13 generated MCP stubs self-connect through the official `mcp` SDK, which must
be importable in the Workspace-private Tool venv that the worker uses. The venv
currently synchronizes only the user-authored `tools/requirements.txt`. The
effective requirements must be the user file plus a Runtime-owned base
dependency, keeping the existing isolation, digest marker, and fail-soft
behavior intact.

### Changes

- In `_ToolEnvironment.reconcile`, compute the effective requirements as the
  user `tools/requirements.txt` plus the Runtime-owned base requirement
  (`mcp`), and synchronize the private venv when either changes.
- Keep the workspace-isolation, atomic marker, and fail-soft error reporting
  unchanged; never fall back to the Host interpreter.
- Add focused tests proving a generated MCP stub imports `mcp` in the worker
  venv and that the base dependency is refreshed independently of user
  requirements changes.
- Record that M14 removes the worker's need for `mcp` (stubs switch to the IPC
  shim), so this base injection is the M13-only dependency surface.

## 中文

### 背景

M13 生成的 MCP 存根通过官方 `mcp` SDK 自连，该包必须能在 worker 使用的
Workspace 私有 Tool venv 中导入。当前 venv 只同步用户编写的
`tools/requirements.txt`。有效 requirements 应为用户文件加 Runtime 拥有的
基础依赖，同时保持现有隔离、digest marker 与 fail-soft 行为不变。

### 变更

- 在 `_ToolEnvironment.reconcile` 中，把有效 requirements 计算为用户
  `tools/requirements.txt` 加 Runtime 拥有的基础依赖（`mcp`），并在任一方
  变化时同步私有 venv。
- 保持 Workspace 隔离、原子 marker 与 fail-soft 错误上报不变；绝不回退到
  Host 解释器。
- 新增聚焦测试：证明生成的 MCP 存根能在 worker venv 中导入 `mcp`，且基础
  依赖独立于用户 requirements 的变化而刷新。
- 记录 M14 将移除 worker 对 `mcp` 的需求（存根改用 IPC shim），因此本注入
  只是 M13 的依赖面。
