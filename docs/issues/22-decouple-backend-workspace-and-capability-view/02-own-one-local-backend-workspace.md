# refactor(runtime): own one Local Backend Workspace

**状态：** resolved

## 背景

定义合同后，Runtime 仍通过 `_prepare_workspace()` 和 `_RuntimeResources` 分别拥有
Host Workspace root、Capability View、Catalog 与 Tool Environment。若不先建立
可工作的 Local Backend Workspace，后续 Handler 迁移只能依赖未接入生命周期的
抽象或同时维护两条执行路径。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，一个 `AgentRuntime` 将明确拥有一个 Local Backend Workspace，所有
Session Kernel 借用同一实例。当前本地 CLI 仍能工作，同时 Runtime open、失败
回收与 close 已具备未来替换 Sandbox/Remote Workspace 的组合点。

## 变更

- 实现 `_LocalBackend`、`_LocalBackendWorkspace` 与基础
  `_LocalWorkspaceFilesystem`。
- Local Workspace root 使用当前 Host absolute path，但只作为 Backend path
  对外暴露；Backend 内部保留真正的 Host `Path`。
- Local Backend 明确拥有 Host ambient environment 的获取与合并策略，后续
  Handler 不再自行读取 `os.environ`；本 issue 先保持当前 Reference CLI 的环境
  可见行为。
- 将 `_RuntimeResources` 收敛为拥有一个 Backend Workspace 及尚未迁移的 Catalog
  资源，不在 `AgentRuntime` 上新增平行 Backend 字段。
- `AgentRuntime._new_kernel()` 向每个 Kernel 注入同一个 Backend Workspace；Kernel
  继续独立拥有 cwd、custom environment、Scheduler 与 Handle。
- Local `flush()` 为显式 no-op，`close()` 幂等；Backend open 失败直接传播，不
  创建 Runtime，也不尝试 fallback。
- 增加 Runtime ownership 测试，覆盖多个 Session 的 shared workspace 与独立
  Session state。

## 验收标准

- [ ] 一个 Runtime 恰好拥有一个 Local Backend Workspace。
- [ ] 多个 Kernel 借用同一 Workspace，且没有 BackendSession。
- [ ] Backend open failure fail closed。
- [ ] 现有 Runtime/Session 生命周期测试保持通过。

