# feat(mcp): reconcile MCP projections at Runtime open / 在 Runtime open 时调和 MCP 投影

**Status / 状态：** pass

## English

### Background

At Runtime open, user-authored `_mcp/<server>/config.json` descriptions must be
aligned into generated Python stub Tools under `.workspace/tools/`, before
`_ToolCatalog.reconcile` scans them. Servers are discovered in parallel; a
failed initial attempt per server is retried up to three times; exhaustion
keeps the Workspace open, emits a Runtime Diagnostic, and leaves no stub for
that server (fail-to-none). Alignment is a full rebuild with no manifest:
stale `mcp_*.py` artifacts are removed first, then a stub is generated for each
successfully discovered server. Removing or renaming a description, or a
discovery failure, therefore removes the previous generated stub. Reconciliation
runs once per Runtime open; creating Sessions does not repeat it.

### Changes

- Create `_capability/mcp/catalog.py` with `_MCPCatalog.reconcile(
  capability_view, on_diagnostic)`; call it in `runtime.py::_reconcile` after
  the Capability View opens and before `_ToolCatalog.reconcile`.
- Read `repertoire/_mcp/*/config.json`, validate each through the facts
  `validate_config` helper; a structurally invalid description is recorded as
  an error, emitted as a diagnostic, and produces no projection.
- Discover servers in parallel with the `mcp` SDK (`ClientSession.list_tools()`)
  using stdio or streamable HTTP; retry a failed initial attempt up to three
  times; on exhaustion emit a diagnostic and skip that server without leaving a
  stub (fail-to-none).
- Align generated artifacts with no manifest: remove every
  `.workspace/tools/mcp_*.py` first, then generate a real Python stub
  `.workspace/tools/mcp_<server>.py` for each successfully discovered server: a
  module docstring with server, transport, and the discovered tool list; one
  typed function per tool with a docstring; a generic `call(tool_name, **kwargs)`;
  and a self-connecting `_call_mcp` that resolves the configured env variable
  NAMES from `os.environ` and never embeds literal values.
- Never touch files without the `mcp_` prefix; a user-authored `mcp_*.py` may be
  removed by the cleanup step (accepted naming-convention deviation, RFC-0005).
- Assert reconcile runs exactly once per Runtime open, discovers servers in
  parallel, and creates no incomplete generated Tool on failure paths.

## 中文

### 背景

Runtime open 时，用户编写的 `_mcp/<server>/config.json` 描述必须对齐为生成的
Python 存根 Tool，写入 `.workspace/tools/`，并先于 `_ToolCatalog.reconcile`
扫描。各 server 并行发现；每 server 首次发现失败重试至多三次；耗尽后
Workspace 照常打开、发一条 Runtime Diagnostic、且该 server 不生成存根
（fail-to-none）。对齐是**无 manifest 的全量重建**：先删除陈旧的 `mcp_*.py`
产物，再为每个成功发现的 server 生成存根。因此删除/改名描述、或发现失败，
都会移除之前的生成存根。Reconciliation 每次 open 只运行一次；创建 Session 不
重复执行。

### 变更

- 创建 `_capability/mcp/catalog.py`，提供
  `_MCPCatalog.reconcile(capability_view, on_diagnostic)`；在
  `runtime.py::_reconcile` 中于 Capability View 打开之后、`_ToolCatalog.
  reconcile` 之前调用。
- 读取 `repertoire/_mcp/*/config.json`，逐条通过 facts 的 `validate_config`
  校验；结构非法的描述记为错误、作为诊断发出、不产生投影。
- 用 `mcp` SDK（`ClientSession.list_tools()`，stdio 或 streamable HTTP）
  **并行**发现各 server；每 server 首次发现失败重试至多三次；耗尽后发诊断并
  跳过该 server，不留存根（fail-to-none）。
- 无 manifest 地对齐生成物：先删除 `.workspace/tools/mcp_*.py`，再为每个成功
  发现的 server 生成真实 Python 存根 `.workspace/tools/mcp_<server>.py`：模块
  docstring 含 server、transport 与发现的工具列表；每个工具生成一个带类型注解
  与 docstring 的函数；提供通用 `call(tool_name, **kwargs)`；自连的
  `_call_mcp` 从 `os.environ` 解析配置的 env 变量名，绝不嵌入字面量值。
- 绝不触碰非 `mcp_` 前缀的文件；用户手写的 `mcp_*.py` 可能被清理步骤删除
  （接受的命名约定偏差，见 RFC-0005）。
- 断言 reconcile 每次 open 恰好执行一次、server 并行发现、失败路径不产生任何
  不完整生成 Tool。
