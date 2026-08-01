# feat(mcp): add the worker-to-Runtime IPC channel / 新增 worker 到 Runtime 的 IPC 回通道

**Status / 状态：** pending

## English

### Background

Generated stubs execute inside an isolated worker process, but MCP calls must
reach the Runtime-owned Workspace MCP Binding. A per-execution worker-to-Runtime
channel is the bridge: the stub writes a structured call, the binding serves it,
and the result returns to the worker.

### Changes

- Establish a per-Tool-execution channel (socketpair or pipe pair) handed to
  the worker through `pass_fds`; a Driver-side task owns the Runtime end.
- Define a minimal JSON framing protocol: a request carries `{server, tool,
  args}`; a response carries `{ok: true, result}` or `{ok: false, code, error}`.
- Correlate request and response per call, enforce a per-call timeout, and
  propagate `kill` and execution cancellation from the worker to the binding
  call.
- Close the channel when the execution ends or the worker exits; a channel
  error maps to a failed Tool result, never to a silent hang.
- Keep the protocol minimal and one-way: it forwards only tool calls and
  returns only their results, with no server-initiated surface.

## 中文

### 背景

生成的存根在隔离的 worker 进程中执行，但 MCP 调用必须到达 Runtime 拥有的
Workspace MCP Binding。每次执行的 worker → Runtime 通道正是桥梁：存根写出
结构化调用，binding 提供服务，结果返回 worker。

### 变更

- 为每次 Tool 执行建立通道（socketpair 或 pipe 对），通过 `pass_fds` 交给
  worker；Driver 侧 task 持有 Runtime 端。
- 定义最小 JSON 帧协议：请求携带 `{server, tool, args}`；响应携带
  `{ok: true, result}` 或 `{ok: false, code, error}`。
- 按调用关联请求与响应，强制单次调用超时，并把 `kill` 与 execution 取消从
  worker 传播到 binding 调用。
- execution 结束或 worker 退出时关闭通道；通道错误映射为失败 Tool result，
  绝不静默挂起。
- 保持协议最小单向：只转发工具调用并只返回其结果，不含任何 server 发起的
  表面。
