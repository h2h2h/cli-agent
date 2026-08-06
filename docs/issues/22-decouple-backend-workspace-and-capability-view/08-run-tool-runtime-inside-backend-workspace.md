# refactor(tools): run Tool Runtime inside Backend Workspace

**状态：** resolved

## 背景

`_ToolEnvironment` 当前在 Host `.workspace` 下创建 venv，并由 `_ToolHandler` 读取
Host Python、package resource worker、Host Tool paths 和 `os.environ` 后创建本地
subprocess。该实现无法让 Tool worker 与 Sandbox Shell 共享 cwd、普通文件或
Capability View。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，Backend Workspace 负责 Tool worker 的 materialization、dependency
reconcile 与 execution。`tools run` 只提交 code、Backend cwd、Session env 和
logical Tool bindings；LocalBackend 继续使用 Workspace-private venv，但该物理
细节不再进入 Handler 或 Runtime resource aggregate。

## 变更

- 将 Tool Environment 重构为 Backend-owned Tool Runtime；Local 实现复用当前 venv
  与 `uv pip compile/sync` 语义。
- Runtime-owned worker 由 Backend materialize 到执行环境，ToolHandler 不读取 Host
  package resource path。
- `_ToolHandler` 为 run 生成 `_ToolExecutionRequest` 并调用
  `BackendWorkspace.prepare_tool()`；list/inspect 继续是 Runtime-local execution。
- Tool request 使用 issue 06 的 logical Tool bindings，不包含 Host Python、venv、
  workspace 或 Tool Path。
- Backend 组合 execution base environment、Session overlay、PATH、VIRTUAL_ENV 与
  worker flags；Handler 不读取 `os.environ`。
- 保持 fresh worker、REPL-style final expression、stdout/stderr、cancel 和
  Tool Environment fail-soft；失败不回退 Host system Python。
- 将剩余 Host process primitive 收入 Local Backend，并增加静态回归，禁止 Command
  Handler 创建 subprocess。

## 验收标准

- [ ] Tool worker 与 Shell/Files 使用同一个 Backend Workspace 和 cwd。
- [ ] ToolHandler 不读取 Host Python、worker Path、Tool Path 或 `os.environ`。
- [ ] dependency failure 继续 fail-soft，且无 Host Python fallback。
- [ ] fresh worker、output、cancel 与 parallel-safe 调度无回归。
