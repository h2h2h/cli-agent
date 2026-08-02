# test(runtime): prove MCP projection and invocation / 验证 MCP 投影与调用

**Status / 状态：** pass

## English

### Background

Focused config, reconcile, cleanup, and dependency tests are necessary, but the
milestone is complete only when the public Runtime path proves the combined
discovery, generation, cleanup, diagnostic, and invocation contract through the
ordinary `tools run` surface — without changing the model-visible Syscall
surface.

### Changes

- Add `tests/test_mcp_projection.py` covering config validation, parallel
  discovery with a scripted local MCP server, retry/exhaustion behavior, stub
  generation (`tools/mcp_<server>.py`), and cleanup by `mcp_` prefix.
- Prove removal: deleting a server description removes its previously generated
  `mcp_*.py` stub, a renamed server replaces the old stub, and a hand-authored
  Tool file without the `mcp_` prefix is never deleted.
- Prove fail-to-none: a server whose discovery exhausts retries emits a
  diagnostic, produces no stub, and its previous stub is removed on the next
  reconcile.
- Add `tests/test_mcp_invocation.py` proving a generated MCP Tool runs through
  `tools run`, including one code block that mixes a local Tool and an MCP Tool,
  and that a connection failure returns an ordinary failed Tool Result without
  deleting the stub.
- Prove Runtime Diagnostics are emitted on retry exhaustion and on invalid
  config without blocking Workspace open.
- Assert the model-visible surface remains exactly `exec`, `output`, `kill`,
  that Runtime public exports are unchanged, and that reconcile does not repeat
  when additional Sessions are created.
- Run the full offline test, lint, and whitespace gates and update
  `docs/handoff.md` and the milestone ticket checklist.

## 中文

### 背景

聚焦的 config、reconcile、清理与依赖测试是必要的，但只有公共 Runtime 路径
通过普通 `tools run` 表面验证组合后的发现、生成、清理、诊断与调用契约，
milestone 才算完成——且不得改变模型可见的 Syscall surface。

### 变更

- 新增 `tests/test_mcp_projection.py`，覆盖 config 校验、用脚本化本地 MCP
  server 的**并行**发现、重试/耗尽行为、存根生成（`tools/mcp_<server>.py`）
  与按 `mcp_` 前缀的清理。
- 验证删除：删除 server 描述移除其先前生成的 `mcp_*.py` 存根、改名 server
  替换旧存根、无 `mcp_` 前缀的用户手写 Tool 文件永不删除。
- 验证 fail-to-none：发现重试耗尽的 server 发诊断、不生成存根，且其先前存根
  在下一次 reconcile 被移除。
- 新增 `tests/test_mcp_invocation.py`：证明生成的 MCP Tool 通过 `tools run`
  运行，包括同一代码段混合本地 Tool 与 MCP Tool，且连接失败返回普通失败 Tool
  Result 而不删除存根。
- 证明重试耗尽与非法 config 时发出 Runtime Diagnostic，且不阻塞 Workspace
  打开。
- 断言模型可见 surface 仍严格为 `exec`、`output`、`kill`，Runtime 公共导出
  不变，且创建更多 Session 时不重复 reconcile。
- 运行完整 offline test、lint 与 whitespace gate，并更新 `docs/handoff.md` 与
  milestone 任务清单。
