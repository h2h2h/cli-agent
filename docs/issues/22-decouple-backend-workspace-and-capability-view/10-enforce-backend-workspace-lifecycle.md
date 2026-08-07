# refactor(runtime): enforce Backend Workspace lifecycle

**状态：** resolved

## 背景

完成各消费者迁移后，Runtime open 仍需要为 Capability binding、MCP、Catalog、Tool
Runtime 与 Library 建立唯一顺序；close 需要先停止 Session Execution 和 Library
worker，再 flush/close Backend Workspace。任一中途失败若没有结构化回收，可能
泄漏进程、Remote job 或产生“已持久化”的错误印象。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，Backend Workspace 成为完整的 Runtime lifecycle resource。Local no-op
flush、未来 Remote flush、partial-open rollback 和 close failure 都有明确语义，
并且任何 Backend failure 都不会隐式回退到权限更宽的 Local execution。

## 变更

- 固定并实现 Runtime open 顺序：Source/State → Backend Workspace/Bound View → MCP
  → Tool Catalog → Tool Runtime → Skill Catalog → Library Catalog → worker。
- 使用显式 resource stack 或等价结构，任何阶段失败时逆序关闭已打开资源。
- 固定 close 顺序：拒绝新 turn → close Kernels/Executions → close Library worker
  → flush Backend Workspace → close Workspace → close Capability State。
- `close()` 保持幂等；flush/close failure 对 Host 可见，不静默报告 persistence
  success，并通过安全 RuntimeDiagnostic 补充非敏感上下文。
- 区分 mandatory Backend open failure 与 Tool Runtime fail-soft；后者仍只禁用
  `tools run`。
- 增加 partial-open、并发 Session close、running execution、worker cancellation、
  flush failure、close failure、重复 close 与 no-fallback 测试。

## 验收标准

- [ ] open/close 顺序与 RFC-0012 一致且有确定性测试。
- [ ] 任一 partial-open failure 不泄漏已创建资源。
- [ ] flush failure 对 Host 可见，不回退或覆盖旧状态。
- [ ] Backend constraint failure 绝不创建 Local execution。

