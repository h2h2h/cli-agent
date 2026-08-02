# refactor(runtime): apply isolated command contexts and remove legacy APIs

**状态：** pending

## 背景

当前 Supervisor 通过 `_DriverKind.TOOL` 特判 Tool context isolation，同时通过
`parallel_safe` 判断其他命令是否需要 Session snapshot。删除 Driver kind 后，
Command 必须直接表达其 Session context 语义。

此外，Parser、Policy、Router、Scheduler、Runtime API 和公共导出中仍会残留旧的
Tool、Driver 和 lane 名称，需要在新模型稳定后一次性清理。

## 影响

完成后，`isolated` 成为 Command 的显式元数据：`cd` 和 `export` 可以顺序修改
Session 状态，Tools 和 parallel-safe command 使用隔离的 cwd/environment snapshot。
生产代码不再依赖 `_DriverKind`、`_ExecutionLane` 或旧的 Tool-specific API。

## 变更

- 将 Supervisor 的 context 创建逻辑改为读取 `route.command.isolated`。
- 对 `parallel_safe=True` 强制使用隔离 context，防止并发命令修改 Session cwd
  或 environment。
- 固定内建命令属性：
  - `cd`：`isolated=False`、`parallel_safe=False`；
  - `export`：`isolated=False`、`parallel_safe=False`；
  - `tools`：`isolated=True`；
  - Shell fallback：`isolated=True`，并按普通 Shell 配置计算并行资格。
- 保持 `cd` 和 `export` 的 Session mutation、取消前不变更、排队取消和 close
  清理语义。
- 从生产代码和测试中删除 `_DriverKind`、`_ExecutionLane`、`route.lane`、
  `_ToolDriver`、`parallel_tools` 和 `tool_parallel_limit`。
- 从 `cli_agent.runtime` 公共导出中移除 `ToolCommand`，保留其 capability-internal
  grammar facts 用途。
- 更新 `AgentRuntime.open`、`AgentRuntime`、`EnvironmentKernel`、Router、Scheduler
  和 Supervisor 的构造参数及文档字符串。
- 增加静态回归检查，确认旧 symbol 不再出现在生产代码中，且 model-visible syscall
  仍只有 `exec`、`output`、`kill`。
