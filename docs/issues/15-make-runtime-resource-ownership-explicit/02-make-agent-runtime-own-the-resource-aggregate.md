# refactor(runtime): make AgentRuntime own the resource aggregate

**Status / 状态：** pass

## 背景

建立 `_RuntimeResources` 后，`AgentRuntime` 仍会暂时保存现有的 Workspace root、
Capability View、Catalog、Tool Environment 与 environment snapshot 平行字段。
如果不完成迁移，新的聚合只会增加一层结构，而不会真正形成所有权边界。

RFC-0006 要求 `AgentRuntime` 只拥有一个 Workspace resource aggregate，同时继续
独立拥有 Host configuration、Session registry 与 Session 关闭流程。Session
Kernel 和 system message assembler 只能借用各自实际需要的字段，不能接收完整
aggregate。

## 影响

完成后，`AgentRuntime` 的 Workspace 状态将从多个 constructor 参数和私有字段
收敛为一个 `_RuntimeResources`。Runtime facade 将更明确地聚焦公开生命周期与
Session 管理，而 `_environment` 仍只依赖具体 Capability 对象，不形成对 Runtime
composition type 的反向依赖。

## 变更

- 在 issue 01 的 `_RuntimeResources` 与 `_reconcile_runtime_resources()` 基础上
  迁移 `AgentRuntime._reconcile()`。
- 将 `AgentRuntime.__init__` 的 Workspace-open 参数替换为一个
  `_RuntimeResources` 参数，并保存为单一私有字段。
- 删除 Runtime 上对应的 Workspace root、Capability View、Tool Catalog、Tool
  Environment、Skill Catalog、base environment 与 `_mcp_catalog` 平行字段。
- 创建 Session system message 时，从 aggregate 显式选择 Workspace root、Tool
  Catalog 与 Skill Catalog。
- `_new_kernel()` 从 aggregate 显式选择 Workspace root、Capability View、Tool
  Catalog、Tool Environment 与 base environment；不要把完整 aggregate 传给
  `EnvironmentKernel`。
- 保持每个 Kernel 从 immutable base environment snapshot 创建独立 mutable copy
  的现有语义。
- 保持 `AgentRuntime.open`、`run_turn`、`close_session`、`close` 的公开调用形态
  以及 Session close 行为不变。
- 更新依赖 Runtime 私有字段的现有测试，使其验证 resource aggregate 或对应的
  公开行为，不保留只为白盒测试服务的 `_mcp_catalog` 状态。
- 运行 Runtime、Session、Workspace、Tool、Skill 与 MCP projection 相关测试，
  确认调和顺序、共享关系和模型可见 surface 均未改变。
