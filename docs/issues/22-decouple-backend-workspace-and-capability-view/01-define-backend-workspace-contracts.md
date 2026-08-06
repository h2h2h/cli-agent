# refactor(backend): define Backend Workspace contracts

**状态：** resolved

## 背景

当前 Command Handler、Capability Catalog 与 Runtime resource 直接传递 Host
`Path`、`_CapabilityView`、`_ToolEnvironment` 和 subprocess closure。RFC-0012 已
批准以一个 Runtime-owned Backend Workspace 同时抽象 execution 与 filesystem，
并明确首期不引入 `BackendSession`。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，后续迁移具有一组可类型检查、可 fake、与具体 Backend 无关的内部合同。
Local、Sandbox 或 Remote 的实现不会进入 Execution State、Snapshot 或 Handler
类型分支；后续 issue 可以逐个迁移消费者而不反复修改核心边界。

## 变更

- 新增私有 Backend domain，定义最小协议与纯 facts：
  - `_Backend` 与异步 `open_workspace()`；
  - `_BackendWorkspace`，包含 root、filesystem、Bound Capability View、Workspace
    MCP runtime、Tool runtime reconcile、flush 与 close；
  - `_WorkspaceFilesystem` 的异步 stat/list/read/write/edit/remove；
  - `_ShellExecutionRequest`、`_ToolExecutionRequest` 与 backend-neutral 文件
    metadata/result。
- Backend path 使用 opaque `str`，合同和 facts 不包含 Host `Path`、file descriptor、
  stat object 或 provider response。
- `prepare_shell()` / `prepare_tool()` 保持同步且无外部副作用；资源创建推迟到
  `PreparedExecution.run()`。
- 不定义 `BackendSession`、Backend kind enum、provider-specific result union 或
  public plugin API。
- 增加 protocol/facts 单元测试与静态依赖测试，证明 `_backend` 不依赖 Kernel、
  Router、Scheduler 或 model-visible protocol。

## 验收标准

- [ ] 合同覆盖 execution 与 filesystem，而不是只覆盖 subprocess。
- [ ] 所有 path/result facts 均为 backend-neutral 数据。
- [ ] Prepared Execution 与现有 Supervisor 合同不变。
- [ ] 不存在 `BackendSession` 或 Backend-specific Snapshot 字段。

