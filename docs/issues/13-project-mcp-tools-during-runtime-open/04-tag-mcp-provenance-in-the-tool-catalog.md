# feat(mcp): tag MCP provenance in the Tool Catalog / 在 Tool Catalog 中标记 MCP provenance

**Status / 状态：** cancelled

> **Cancelled / 取消：** 本 issue 不再需要。经 RFC-0005 决策，MCP 存根以
> `mcp_<server>.py` 命名、按普通 Workspace Tool 呈现，不引入 `mcp` provenance
> 值。来源识别与清理只依赖 `mcp_` 文件名前缀约定与模块 docstring，不扩展
> `ToolEntry.provenance`。

## English

### Background

Generated MCP stubs are real files in the Workspace layer, so the Tool Catalog
would otherwise report them as ordinary `workspace` Tools. They must be
distinguishable from hand-authored Workspace files.

### Changes

- **Cancelled.** RFC-0005 instead distinguishes MCP artifacts by the `mcp_`
  filename prefix and the module docstring (server name, transport). No
  `ToolEntry.provenance` extension, no manifest lookup, no index/info source
  column. MCP stubs appear as ordinary Workspace Tools.

## 中文

### 背景

生成的 MCP 存根是 Workspace 层的真实文件，因此 Tool Catalog 若不处理会把它
报告为普通 `workspace` Tool。它们需要能与手写 Workspace 文件区分。

### 变更

- **已取消。** RFC-0005 改为用 `mcp_` 文件名前缀与模块 docstring（server 名、
  transport）区分 MCP 生成物。不扩展 `ToolEntry.provenance`、不做 manifest
  查询、不在 index/info 增加来源列。MCP 存根按普通 Workspace Tool 呈现。
