# refactor(mcp): run Workspace MCP inside Backend Workspace

**状态：** resolved

## 背景

当前 MCP Catalog discovery 在 Host Runtime 中连接 stdio/http server，生成的 Tool
stub 又在 Host Tool worker 中自连接。RFC-0012 已决定 Workspace `_mcp` 的
discovery 与 invocation 都位于 Backend Workspace；这也取代 RFC-0005 中拟议的
Host Runtime IPC binding 执行位置。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，Workspace MCP executable、网络策略、cwd、Tool worker 和 capability 文件
处于同一 Backend 命名空间。Host-owned MCP 若未来引入，将作为另一类显式
capability，而不会隐式复用 Workspace `_mcp` 配置。

## 变更

- 实现 `_WorkspaceMCPRuntime` 子协议与 LocalBackend 实现，返回
  provider-neutral server/tool discovery facts。
- MCP Catalog 通过 Backend runtime discovery，不持有 Host transport stream、
  client 或 subprocess。
- 将 invocation binding materialize 到 Backend Tool Runtime；generated stubs 在
  Backend worker 中调用该 binding，不回连 Host Runtime IPC。
- 保持 config validation、stdio/http transport、retry、diagnostic、stub full
  rebuild 与普通 Tool Catalog projection。
- 明确 MCP env name/secret injection：Backend 只接收本次 invocation 所需值，
  credential 不进入日志、diagnostic、generated stub 或持久 snapshot。
- 更新 RFC-0005 的冲突部分，标明 Host IPC placement 已由 RFC-0012 supersede；
  保留 bounded concurrency、可信配置与 projection 等仍适用目标。
- 增加 Local stdio fixture、HTTP fake、discovery failure、credential redaction 和
  Tool worker invocation 端到端测试。

## 验收标准

- [ ] Workspace MCP discovery 与 invocation 均通过 Backend Workspace。
- [ ] Runtime/Catalog 不直接创建 Workspace MCP subprocess。
- [ ] credential 不进入 stub、日志、diagnostic 或 snapshot。
- [ ] MCP Tool 继续通过普通 Tool execution lifecycle 和 scheduling。

