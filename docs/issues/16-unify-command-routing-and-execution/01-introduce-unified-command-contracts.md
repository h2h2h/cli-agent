# refactor(runtime): introduce unified Command contracts

**状态：** resolved

## 背景

当前 Environment 使用 `_DriverKind`、`_ExecutionDriver` 和
`_CustomCommandSpec` 分别表达命令分类、执行准备与 Custom command。Shell、Custom
和 Tool 的执行能力因此分散在不同抽象中，路由结果还需要额外携带 driver 与
driver kind。

RFC-0007 选择以 `_Command` 作为统一的命令描述与执行准备合同，由
`_ShellCommand` 和 `_CustomCommand` 派生。Command 负责匹配、parallel-safe 判断
和 prepare；一次具体运行仍由独立的 Prepared Execution 负责生命周期。

参考：[RFC-0007](../../rfcs/proposed/RFC-0007-unified-command-routing-and-execution-refactor.md)。

## 影响

完成后，Router、Scheduler 和 Supervisor 可以依赖统一的 Command 合同，不再根据
Tool 或 driver 类型添加特殊分支。`_ExecutionRoute` 只需要绑定已解析的 Command
和当前命令的 `parallel_safe` 结果，为后续删除 `_DriverKind` 和 `_ExecutionLane`
建立基础。

## 变更

- 在 `src/cli_agent/runtime/_environment/commands.py` 中定义 private `_Command`
  抽象合同。
- 定义 `_ShellCommand` 和 `_CustomCommand`，统一提供：
  - command name 或 Shell fallback 标识；
  - `matches()`；
  - `parallel_safe()`；
  - `isolated`；
  - `prepare()`。
- 将现有 `_CustomCommandSpec` 的 name、prepare 与调度属性迁移到
  `_CustomCommand`，不保留两套 Custom command 描述类型。
- 将 `_ExecutionRoute` 改为持有 Command 和 `parallel_safe`，不再新增 driver kind
  或 Tool-specific route 字段。
- 让 `_CommandRouter` 先查 Custom registry，未命中时返回唯一的 Shell fallback
  Command。
- 保持 `prepare()` 不启动进程、不修改 Session 状态，只构造 Prepared Execution。
- 增加 Command 合同的单元测试，覆盖 Custom 命中、Shell fallback、parallel-safe
  计算和 isolated 元数据。
