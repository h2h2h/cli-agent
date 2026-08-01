# test(runtime): prove bounded MCP invocation / 验证有界的 MCP 调用

**Status / 状态：** pending

## English

### Background

Focused binding, budget, channel, shim, and route tests are necessary, but the
milestone is complete only when the public Runtime path proves shared, bounded,
isolated MCP invocation with correct failure and cleanup semantics — without
changing the model-visible Syscall surface.

### Changes

- Add `tests/test_mcp_binding.py`: a shared `ClientSession` across Sessions,
  concurrent calls within the budget, an explicit `MCP_BUSY` result on a full
  waiting queue, and Workspace isolation proving two Workspaces never share
  clients, credentials, processes, or connections.
- Prove Runtime close terminates every MCP server process the binding launched,
  idempotently, including on failed open paths.
- Extend `tests/test_mcp_invocation.py`: a generated MCP Tool invoked through
  `tools run`, one code block mixing a local Tool and an MCP Tool, and
  disconnection returning an ordinary failed Tool Result without deleting the
  stub.
- Prove `kill`/execution cancellation propagates from the worker to the binding
  call, and that a channel error maps to a failed Tool result instead of a hang.
- Prove the minimal-client scope: no sampling, elicitation, or roots surface is
  advertised.
- Assert the model-visible surface remains exactly `exec`, `output`, `kill` and
  that Runtime public exports are unchanged.
- Run the full offline test, lint, and whitespace gates and update
  `docs/handoff.md` and the milestone ticket checklist.

## 中文

### 背景

聚焦的 binding、预算、通道、shim 与路由测试是必要的，但只有公共 Runtime 路径
验证共享、有界、隔离的 MCP 调用及正确的失败与清理语义，milestone 才算完成——
且不得改变模型可见的 Syscall surface。

### 变更

- 新增 `tests/test_mcp_binding.py`：跨 Session 共享 `ClientSession`、预算内并发
  调用、等待队列满时返回显式 `MCP_BUSY`，以及 Workspace 隔离——两个 Workspace
  绝不共享 client、凭据、进程或连接。
- 证明 Runtime close 幂等地终止 binding 启动的每个 MCP server 进程，包括 open
  失败路径。
- 扩展 `tests/test_mcp_invocation.py`：生成的 MCP Tool 通过 `tools run` 调用、
  同一代码段混合本地 Tool 与 MCP Tool、断连返回普通失败 Tool result 而不删除
  存根。
- 证明 `kill`/execution 取消从 worker 传播到 binding 调用，且通道错误映射为
  失败 Tool result 而非挂起。
- 证明 minimal-client 范围：不宣传 sampling、elicitation 或 roots 表面。
- 断言模型可见 surface 仍严格为 `exec`、`output`、`kill`，Runtime 公共导出
  不变。
- 运行完整 offline test、lint 与 whitespace gate，并更新 `docs/handoff.md` 与
  milestone 任务清单。
