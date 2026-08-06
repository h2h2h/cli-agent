# refactor(capability): separate Capability Source, State, and Bound View

**状态：** resolved

## 背景

当前 `_CapabilityView` 同时解析 Repertoire、维护 Workspace upper/whiteout、推导
provenance，并用 Host symlink、copy-up 和 Shell mutation lock 物化有效 View。
RFC-0012 要求逻辑 capability 输入和状态独立于某个 Backend 的物理呈现。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，Capability lower、持久 upper/whiteout facts 与 Local 物化机制具有明确
边界。Catalog 可以依赖 Bound View 的可信 facts，Handler 不再看到 symlink 或
copy-up API；未来原生 overlay 或 Remote materialization 无需复用 Local 算法。

## 变更

- 定义 `_CapabilitySource`、`_CapabilityState`、`_BoundCapabilityView` 与共享
  `_CapabilityInspection` facts。
- Host-owned Source/State 可以使用 Host `Path` 读取 Repertoire 和持久状态；Bound
  View 只暴露 Backend root、relative path 与 backend-neutral inspect/list/read。
- 将 file-level symlink attach、stale link 清理、copy-up、whiteout 与 mutation
  lock 收入 Local Backend 的 Bound View 实现。
- 让 Local Workspace Filesystem 对 managed capability write/edit 自动执行 copy-up
  和 whiteout 调和；FileHandler 不调用 `prepare_path()`。
- 让 Local Shell preparation 在 Backend 内调用 Bound View 的 Local mutation
  hook；通用 Backend 合同不出现 `prepare_shell()` capability API。
- 不保留旧 `_CapabilityView` concrete API 的兼容 wrapper 或 re-export。
- 迁移 RFC-0002 的 provenance、shadow、whiteout、invalid link 与并发 copy-up 测试。

## 验收标准

- [ ] Capability Source/State 与 Local Bound View 是不同 owner 和类型。
- [ ] 通用 Handler 不依赖 symlink、copy-up、whiteout 或 Host Path。
- [ ] Local provenance 与 lower/upper/whiteout 行为保持完整测试覆盖。
- [ ] Bound View 合同可由不使用 symlink 的 fake Backend 实现。

