# feat(mcp): build the Workspace MCP Binding / 构建 Workspace MCP Binding

**Status / 状态：** pending

## English

### Background

M13 stubs self-connect per call, so the Runtime owns no live MCP state and
cannot share clients, bound concurrency, or clean up connections on close. M14
requires one Workspace-bound AgentRuntime to own the live MCP clients,
connections, server processes, and mutable server state, with Sessions sharing
them and different Workspaces never sharing anything.

### Changes

- Create `_capability/mcp/binding.py` with `_WorkspaceMCPBinding`, constructed
  at Runtime open from the reconciled server descriptions.
- Open one shared `ClientSession` per configured server at Runtime open (stdio
  subprocess or streamable HTTP), reusing the `mcp` SDK used by M13 discovery.
- Sessions in one Runtime share the binding and may issue concurrent requests
  through it; different Workspaces get independent bindings with no shared
  credentials, processes, connections, or mutable server state.
- Keep the client minimal and one-way: only `list_tools` and `call_tool` are
  used; no sampling, elicitation, roots, or other Server-initiated model or
  Workspace access is advertised.
- Runtime close closes every session and terminates every MCP server process
  the binding launched; close is idempotent and also runs on failed open paths.
- A server disconnection is reported as an ordinary failed Tool result; a call
  failure never deletes the generated Tool.

## 中文

### 背景

M13 存根每次调用自连，因此 Runtime 不拥有任何 live MCP 状态，无法共享
client、约束并发或在 close 时清理连接。M14 要求一个 Workspace 绑定的
AgentRuntime 拥有 live MCP client、连接、server 进程与可变 server 状态，Session
共享它们，而不同 Workspace 之间绝不共享任何东西。

### 变更

- 创建 `_capability/mcp/binding.py`，提供 `_WorkspaceMCPBinding`，在 Runtime
  open 时依据调和后的 server 描述构建。
- 在 Runtime open 时为每个已配置 server 打开一个共享 `ClientSession`（stdio
  子进程或 streamable HTTP），复用 M13 发现所用的 `mcp` SDK。
- 同一 Runtime 的 Session 共享该 binding，并可并发发起请求；不同 Workspace
  获得独立 binding，不共享凭据、进程、连接或可变 server 状态。
- 保持 client 单向最小：只使用 `list_tools` 与 `call_tool`；不宣传
  sampling、elicitation、roots 或其他 Server 发起的模型/Workspace 访问。
- Runtime close 关闭每个连接并终止 binding 启动的每个 MCP server 进程；close
  幂等，且在 open 失败路径同样执行。
- server 断连按普通失败 Tool result 报告；调用失败永不删除生成的 Tool。
