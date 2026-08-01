# feat(mcp): bound MCP concurrency per server / 为每个 MCP server 约束并发

**Status / 状态：** pending

## English

### Background

The architecture contract defines an MCP Concurrency Budget per server covering
both in-flight requests and queued waiters: requests within the budget stay
concurrent, and a full waiting queue fails immediately with an explicit
`MCP_BUSY` result rather than growing without limit. Exact defaults are left to
conformance.

### Changes

- Add a per-server `MCPConcurrencyBudget` to the binding with a bounded
  in-flight count and a bounded waiting queue.
- Requests within the budget run concurrently through the shared
  `ClientSession`; a full waiting queue returns an explicit `MCP_BUSY` result
  immediately.
- A transport that cannot multiplex safely serializes only its own connection;
  the Runtime does not impose a global MCP FIFO across servers.
- Make the budget Host-configurable, with proposed defaults of 4 in-flight and
  32 queued waiters (pending conformance confirmation).
- `MCP_BUSY` and budget behavior are covered by focused tests that saturate the
  budget with a scripted server.

## 中文

### 背景

架构契约定义了每 server 的 MCP Concurrency Budget，同时覆盖 in-flight 请求与
排队等待者：预算内请求保持并发，满队时立即以显式 `MCP_BUSY` 结果失败，而不是
无界增长。具体默认值留给 conformance 确定。

### 变更

- 为 binding 增加每 server 的 `MCPConcurrencyBudget`，包含有界的 in-flight
  计数与有界等待队列。
- 预算内请求通过共享 `ClientSession` 并发执行；等待队列满时立即返回显式
  `MCP_BUSY` 结果。
- 无法安全多路复用的传输只串行化其自身连接；Runtime 不在跨 server 之间强加
  全局 MCP FIFO。
- 预算对 Host 可配置，建议默认 in-flight=4、排队=32（待 conformance 确认）。
- 用脚本化 server 打满预算，对 `MCP_BUSY` 与预算行为做聚焦测试。
