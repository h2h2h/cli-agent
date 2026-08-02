# refactor(runtime): decouple Tool facts from command parsing

**状态：** resolved

## 背景

`CommandParseResult` 当前包含 `tool: ToolCommand | None`，Kernel 会在 Policy
之前调用 `classify_tool_command()`，然后由 Policy、Approval Request、Router 和
Custom registry 读取该字段。这让通用 Shell parser 依赖 Tools capability grammar，
也使一次普通命令解析隐含了 capability classification。

参考：[RFC-0007](../../rfcs/proposed/RFC-0007-unified-command-routing-and-execution-refactor.md)。

## 影响

完成后，Parser 只负责产生不可变的通用语法事实，Policy 只根据这些事实授权，
Tool grammar 只在 `tools` Custom command 内部产生 `ToolCommand`。通用执行链将不再
因是否包含 Tool invocation 而改变 parse result 的结构。

## 变更

- 从 `CommandParseResult` 中移除 `tool` 字段。
- 移除 `command_parser.py` 对 `ToolCommand` 的导入。
- 将 `classify_tool_command()` 改为返回 `ToolCommand | None` 的纯函数，例如
  `parse_tool_command()`，不得通过 `dataclasses.replace()` 修改通用 parse result。
- 从 Kernel 中移除：
  - `classify_tool_command` import；
  - parse 后的 Tool enrichment；
  - 对 `command.tool` 的判断与一致性检查。
- 从 `ExecutablePolicy` 中移除 Tool-specific allow 分支和 `tool.*` rule。
- 保留默认 Policy 对 `tools` 的普通 executable 处理：默认 allow，显式 deny/ask
  仍由 Host policy 决定。
- 从 `ExecutionApprovalRequest` 中移除 `tool` 字段及其构造逻辑。
- 保持 Policy、Approval 和 ExecutionDecision 仍绑定同一个完整的
  `CommandParseResult`。
- 更新所有 Parser、Policy、Approval、Kernel 和公共 surface 测试。
