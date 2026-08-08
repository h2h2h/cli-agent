# test(backend): prove Backend Workspace decoupling end to end

**状态：** resolved

## 背景

issue 01-10 分别迁移合同、Runtime ownership、Shell、Files/cd、Capability View、
Catalog、Library、Tool Runtime、MCP 与 lifecycle。单模块测试不能证明所有操作确实
共享同一个非 Host 命名空间，也不能阻止后续 Handler 或 Catalog 重新引入 Host
Path/subprocess 快捷路径。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，RFC-0012 的边界由 contract suite、第二 Backend proof、静态依赖测试和
架构文档共同固定。后续新增真正的 Sandbox/Remote provider 可以复用同一套验收，
无需修改模型可见协议或 Execution Supervisor。

## 变更

- 增加一个 deterministic non-Host-mirror Backend fake 或最小 Sandbox proof，验证：
  - Shell 写入可被 Files、Tool、Tool/Skill/Library Catalog 读取；
  - Files/Tool 写入对后续 Shell 可见；
  - Bound Capability provenance 不依赖 symlink；
  - 两个 Kernel 共享文件但不共享 cwd/env/Handle；
  - Backend open/constraint failure 不回退 Local。
- 增加静态回归：
  - Command Handler 不创建 subprocess；
  - Handler/Catalog 不使用 Host `Path` 访问 live Workspace；
  - `_ExecutionState`、Snapshot 与 protocol 无 Backend discriminator；
  - Runtime 不存在 `BackendSession` 或平行 Workspace owner。
- 覆盖 queued/running cancel、Runtime close、Tool fail-soft、MCP invocation、Library
  worker 和 Capability lower/upper/whiteout 的跨层组合。
- 更新 `docs/architecture.md`、README、安全说明及相关 RFC/discussion 的 supersede
  关系，明确 LocalBackend 不提供 OS isolation。
- 运行完整 pytest、Ruff、mypy（若项目配置可用）与 diff check，记录结果供同行
  评审。
- 所有子 issue 通过 peer review 后，将对应状态改为 `resolved`，并同步 RFC-0012
  的实现状态；不自动提交代码。

## 验收标准

- [ ] 第二 Backend proof 覆盖 Shell、Files、Tool、Catalog 与 Library 的同命名空间。
- [ ] 固定 syscall、Supervisor、Snapshot、Session isolation 与调度行为无回归。
- [ ] 静态回归阻止 Host Path/subprocess 与 Backend type branch 返回 Handler。
- [ ] 完整质量检查通过并更新架构文档。
