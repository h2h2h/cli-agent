# feat(tools): declare Tool parallel safety in the catalog

**状态：** pending

## 背景

当前 Tool 是否允许并行由 Runtime 的 `parallel_tools` 名称集合决定。该配置无法
随 Tool 文件一起发现，且不能表达 Tool 自身的调度能力事实，也无法区分普通 Tool
与 MCP Tool 的默认并行策略。

用户在 repertoire 或 Workspace 中注册的 Tool 需要能够声明自身是否可以被并行
调度。该声明应在 Runtime open 时随有效 Capability View 文件一起进入 Tool Catalog，
而不是在执行时 import Tool 模块或读取生成的 `index.md`。

## 影响

完成后，Tool Catalog 会同时保存 Tool 的验证、来源和调度事实。用户注册的普通
Tool 缺少声明时默认可并行；MCP Tool 缺少声明时默认不可并行。只有所有静态引用
的 Tool 都满足 parallel-safe，`tools run` 才能获得并行资格。声明解析失败时，
Catalog 使用对应默认值并通过 `RuntimeDiagnostic` 提示，不阻断 Runtime open。
`parallel_tools` 和 `tool_parallel_limit` 被删除，Scheduler 只保留一个统一的
`parallel_limit`。

## 变更

- 为 `ToolEntry` 增加 `parallel_safe: bool` 字段。
- 规定 Tool 文件使用模块级声明：

  ```python
  PARALLEL_SAFE = True
  ```

- 在 Tool Catalog 现有 AST inspection 阶段解析该声明：
  - 只接受顶层 `Assign` 或 `AnnAssign`；
  - 只接受字面量 `True` 或 `False`；
  - 普通 Tool 缺失声明默认为 `True`；
  - MCP Tool 缺失声明默认为 `False`；
  - 非布尔值、重复声明或源代码解析失败时使用对应默认值，并发送
    `tools.parallel_safe_parse_failed` 诊断；
  - 不 import、不执行 Tool 模块。
- Workspace effective Tool 覆盖 repertoire Tool 时，以 effective file 的声明为准。
- 在 `tools/index.md` 和 `tools info` 中展示 parallel-safe 状态；生成 projection
  仍不能作为 Runtime authority。
- 修改 Tools command 的并行判断：
  - `list` 和 `info` 固定可并行；
  - `run` 必须 valid、存在静态引用、没有 dynamic reference，且每个引用的
    `ToolEntry.parallel_safe` 都为 true；
  - invalid、无引用、动态引用或任一 Tool 的 `parallel_safe` 为 false 时串行。
- 删除 AgentRuntime、EnvironmentKernel、CommandRouter 和测试中的
  `parallel_tools`、`tool_parallel_limit` 参数及校验逻辑。
- 更新 Runtime API 文档、system message、Tool Catalog tests 和 parallel scheduling
  tests，明确该声明是调度信任而不是安全认证。
