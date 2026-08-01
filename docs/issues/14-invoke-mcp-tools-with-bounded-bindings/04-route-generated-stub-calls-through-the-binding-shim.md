# feat(mcp): route generated stub calls through the binding shim / 让生成的存根调用经 binding shim 路由

**Status / 状态：** pending

## English

### Background

M13 stubs self-connect through the venv `mcp` package on every call, so the
Runtime owns no live traffic. M14 switches stub internals to route through a
worker-injected shim that forwards over the IPC channel, so every MCP call is
owned by the Workspace MCP Binding. The generated stub surface must stay
unchanged so model-facing behavior and composition are stable.

### Changes

- Add a fixed worker-side `cli_agent_mcp` shim shipped with the Runtime that
  reads and writes the per-execution channel and is importable by generated
  stubs.
- Regenerate M13 stubs so `_call_mcp` calls the shim instead of
  self-connecting; keep the stub surface (typed functions, docstrings, generic
  `call(tool_name, **kwargs)`) identical.
- Remove the M13 self-connect code path and the Runtime-injected `mcp` base
  dependency from the Tool Environment, since the worker no longer needs the
  `mcp` package.
- Ensure the shim exposes only the one-way call/result surface and fails
  closed when the channel is missing or closed.

## 中文

### 背景

M13 存根每次调用都通过 venv 里的 `mcp` 包自连，因此 Runtime 不拥有任何 live
流量。M14 把存根内部实现改为经 worker 注入的 shim 转发到 IPC 通道，使每个 MCP
调用都由 Workspace MCP Binding 拥有。生成的存根表面必须保持不变，以保证面向
模型的行为与组合能力稳定。

### 变更

- 新增随 Runtime 分发的固定 worker 侧 `cli_agent_mcp` shim，读写每次执行的
  通道，并可由生成的存根导入。
- 重新生成 M13 存根，使 `_call_mcp` 调用 shim 而非自连；保持存根表面（带类型
  注解的函数、docstring、通用 `call(tool_name, **kwargs)`）完全一致。
- 移除 M13 自连代码路径，并从 Tool Environment 移除 Runtime 注入的 `mcp` 基础
  依赖——worker 不再需要 `mcp` 包。
- 确保 shim 只暴露单向的调用/结果表面，且通道缺失或已关闭时失败关闭。
