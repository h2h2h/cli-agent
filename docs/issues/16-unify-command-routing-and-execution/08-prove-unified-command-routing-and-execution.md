# test(runtime): prove unified command routing and execution

**状态：** pending

## 背景

这次重构同时改变 Parser、Policy、Command registry、Tool Catalog、Scheduler、
Supervisor 和私有模块布局。单独的单元测试无法证明完整 Runtime 路径已经从
`exec` 经过统一 Policy、Custom-first routing、全局 Scheduler 进入正确的
Prepared Execution。

需要通过聚焦测试和文档回归，证明 Tools、Shell、`cd`、`export` 在同一个 Execution
lifecycle 下协同工作，并证明删除 Tool lane 后没有破坏 Session 隔离、取消、关闭
和结果顺序。

## 影响

完成后，RFC-0007 的关键结构约束将由测试和文档共同固定：非法 Tools 命令不能
fallback 到 Shell，Tool metadata 能正确影响并行资格，serial barrier 和 isolated
context 语义可观察，旧的 Driver/lane API 不会被后续修改重新引入。

## 变更

- 扩展 Parser/Policy/Approval 测试，验证：
  - `CommandParseResult` 不含 `tool`；
  - Policy 只消费通用 parse facts；
  - Approval Request 不含 Tool-specific 字段；
  - Policy deny/ask 发生在 Custom route 和 Execution admission 之前。
- 扩展 Custom routing 测试，验证：
  - `cd`、`export`、`tools` 命中 Custom；
  - 普通 command 命中 Shell fallback；
  - malformed `tools`、pipeline、redirection 和非法参数不落 Shell；
  - reserved command name 冲突按约定失败。
- 扩展 Tool Catalog 测试，覆盖：
  - `PARALLEL_SAFE` 缺失、true、false、非法值和重复声明；
  - repertoire 与 Workspace override；
  - index/info 展示与 authority 隔离；
  - 静态引用、动态引用、invalid Tool 对并行资格的影响。
- 扩展 Scheduler 测试，覆盖：
  - 单一 `parallel_limit`；
  - Shell、Custom、Tool 混合并行；
  - serial barrier；
  - queue limit、queued kill、running kill、preparation failure 和 close。
- 扩展 Supervisor/Kernel 集成测试，验证：
  - `cd`、`export` 的 Session mutation 顺序；
  - parallel-safe command 使用隔离 snapshot；
  - Tools worker 不修改 Session cwd/environment；
  - Tool output、cancel、fresh worker 和失败结果不回归。
- 更新 `docs/architecture.md`，移除 Tool lane、DriverKind 和旧 drivers 关系，
  展示 Command、Handlers、Scheduler 和 Tool Catalog 的新边界。
- 更新 RFC-0003 和相关 discussions，明确 Tool lane、`parallel_tools` 与
  `CommandParseResult.tool` 已由 RFC-0007 supersede。
- 运行完整 pytest、Ruff、mypy 和 diff check，并记录结果供同行评审。

## 验证记录

- 增加统一生命周期回归，覆盖 `cd`、`export`、`tools` 和 Shell fallback
  在同一个 Execution Snapshot、取消和 Session 状态模型下运行。
- 增加生产源码、Runtime 目录和架构文档静态回归，防止旧 Driver/lane API
  重新进入实现。
- 完整 pytest、Ruff 和 issue 相关 diff check 通过；项目当前未配置 mypy
  依赖或 mypy 配置，无法执行 mypy 检查。
