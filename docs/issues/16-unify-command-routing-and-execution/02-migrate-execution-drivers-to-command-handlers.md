# refactor(runtime): migrate execution drivers to command handlers

**状态：** resolved

## 背景

当前 `drivers/` 目录同时存放 Shell Driver、Tool Driver、Inline/Process
Execution、Driver contracts，以及 `commands.py` 中的 `cd` 和 `export` prepare。
目录名已经无法准确表达其中既有命令 handler 又有通用执行基础设施的事实。

RFC-0007 建议将目录更名为 `handlers/`，让命令级执行实现集中在该目录，而把
Session Execution State 保留在现有 `execution.py` 中。

参考：[RFC-0007](../../rfcs/proposed/RFC-0007-unified-command-routing-and-execution-refactor.md)。

## 影响

完成后，Command 抽象和 Registry 只负责组合命令，具体的 Shell、Tools、`cd`、
`export` 与 process execution 实现拥有清晰的模块归属。后续新增 Custom command
时可以新增 handler，而不需要扩展 Router 或复用不准确的 Driver 命名。

## 变更

- 将 `src/cli_agent/runtime/_environment/drivers/` 更名为 `handlers/`。
- 将通用 contracts 迁移到 `handlers/base.py`，并将 `_DriverContext` 更名为
  `_CommandContext`。
- 将 `_DriverExecution` 更名为 `_PreparedExecution`，保留统一的 run/cancel
  lifecycle。
- 将 `drivers/executions.py` 迁移为 `handlers/executions.py`。
- 将 Shell prepare 与 Capability View mutation preparation 迁移到
  `handlers/shell.py`。
- 将 `_prepare_cd` 迁移到 `handlers/cd.py`。
- 将 `_prepare_export` 迁移到 `handlers/export.py`。
- 暂时将现有 Tool execution 实现迁移到 `handlers/tools.py`，为后续删除 Tool
  special route 做准备。
- 更新 `execution.py`、`supervisor.py`、Kernel 和所有测试的 imports 与字段命名。
- 删除旧 `drivers/` 包及其生产代码引用，不保留兼容 re-export。
- 验证现有 Shell、`cd`、`export`、Inline Execution、Process Execution 和取消
  行为不发生回归。
