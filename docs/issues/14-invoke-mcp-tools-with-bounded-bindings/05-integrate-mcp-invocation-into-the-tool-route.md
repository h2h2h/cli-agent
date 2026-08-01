# feat(mcp): integrate MCP invocation into the Tool route / 将 MCP 调用接入 Tool 路由

**Status / 状态：** pending

## English

### Background

The impact matrix constraint for milestone 14 is explicit: generated MCP Tool
invocation must enter through the Tool route and the common Policy gate while
preserving MCP budgets. The model-visible surface stays exactly `exec`,
`output`, and `kill`, and `tools run` remains the invocation form, allowing a
code block to mix local and MCP Tools.

### Changes

- Have the Tool Driver detect runs that reference MCP-backed tools and attach
  the IPC channel to every worker spawn (always-carry policy), passing the
  channel fd and making the `cli_agent_mcp` shim importable.
- Let the existing static reference analysis in `_tool_facts` yield the
  `tools.<server>` module references; MCP references participate in the same
  parallel-safety analysis with the Host `parallel_tools` allowlist and
  additionally honor the per-server binding budget.
- Support mixed local/MCP code blocks in the unified worker; MCP results return
  as ordinary Execution output through the existing wait/output/kill lifecycle.
- Define failure semantics: server disconnection, `MCP_BUSY`, and channel errors
  all return an ordinary failed Tool Result; no failure deletes the generated
  stub.
- Keep the Syscall surface and Runtime public exports unchanged.

## 中文

### 背景

milestone 14 的影响矩阵约束很明确：生成的 MCP Tool 调用必须进入 Tool 路由与
公共 Policy gate，同时保留 MCP 预算。模型可见 surface 仍严格为 `exec`、
`output`、`kill`，`tools run` 仍是调用形态，并允许一个代码段混合本地与 MCP
Tool。

### 变更

- 让 Tool Driver 识别引用 MCP-backed Tool 的 run，并给每次 worker spawn 附加
  IPC 通道（always-carry 策略），传入通道 fd 并让 `cli_agent_mcp` shim 可导入。
- 让 `_tool_facts` 中既有的静态引用分析产出 `tools.<server>` 模块引用；MCP
  引用参与同一套并行安全分析（Host `parallel_tools` allowlist），并额外受每
  server binding 预算约束。
- 在统一 worker 中支持本地/MCP 混合代码段；MCP 结果经既有 wait/output/kill
  生命周期作为普通 Execution 输出返回。
- 定义失败语义：server 断连、`MCP_BUSY` 与通道错误都返回普通失败 Tool result；
  任何失败都不删除生成的存根。
- 保持 Syscall surface 与 Runtime 公共导出不变。
