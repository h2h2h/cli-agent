# test(runtime): prove Runtime resource ownership boundaries

**Status / 状态：** pass

## 背景

资源聚合和 Runtime 迁移主要改变私有结构，现有端到端行为测试不足以阻止后续代码
重新引入平行 Workspace 字段、把 Host-owned dependency 放入 aggregate，或让
`_environment` 直接依赖 Runtime composition type。RFC-0006 还要求架构图和交接
文档准确表达 Workspace、Runtime 与 Session 的 owner/borrower 关系。

## 影响

完成后，资源所有权不仅由实现约定表达，还会受到聚焦测试和架构文档约束。后续
修改能够在评审和 CI 中发现 public surface 泄漏、依赖方向反转、Session 错误持有
共享资源或虚构 cleanup 等回归。

## 变更

- 扩展 `tests/test_agent_runtime.py`，验证：
  - 一个 Runtime 只保存一个 `_RuntimeResources`；
  - 多个 Session 借用同一个 Capability View、Tool Catalog 与 Tool Environment；
  - 每个 Session 仍获得独立的 environment copy；
  - `close_session()` 和 Runtime close 只关闭 Session-owned state；
  - Host-owned Provider、Policy、Approver 与诊断回调不会进入 resource aggregate
    或被 Runtime 关闭。
- 扩展 `tests/test_public_surface.py`，断言 `_RuntimeResources` 与
  `_reconcile_runtime_resources` 不进入 `cli_agent.runtime.__all__`。
- 增加轻量架构测试，断言：
  - `_capability` 不导入 `_resources`；
  - `_resources` 不导入 `_environment`；
  - `EnvironmentKernel` 不接收完整 `_RuntimeResources`。
- 更新 `docs/architecture.md`，标明 `AgentRuntime` 拥有一个 Workspace resource
  aggregate，Session 只借用其中的明确对象。
- 更新 `docs/handoff.md`，记录 RFC-0006 的实现状态、资源字段范围和非目标。
- 运行完整 pytest、Ruff、类型检查与 whitespace gate，并记录结果供同行评审。
