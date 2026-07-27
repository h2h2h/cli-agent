# refactor(package): adopt the Agent Runtime package layout / 采用 Agent Runtime 包布局

**Status / 状态：** pass

## English

### Background

The Runtime is a core component of cli-agent rather than a separate top-level product. It belongs under the `cli_agent` package while `AgentLoop` and `EnvironmentKernel` remain private. Establishing that package seam prevents later work from extending a generic top-level `runtime` import surface.

This work starts only after the external architecture gate for the parent issue has been accepted. It changes package organization without adding Tool Call or command-execution behavior.

### Changes

- Move the existing model types, Agent Loop, and provider code under `src/cli_agent/runtime`.
- Update internal imports, tests, and packaging configuration to use `cli_agent.runtime`.
- Keep implementation modules such as `_agent_loop` private.
- Export only the provider-neutral model types and official provider adapters that already form part of the public interface.
- Add a public-import contract test and preserve all existing text-only behavior.

## 中文

### 背景

Runtime 是 cli-agent 的核心组件，而不是独立的顶层产品。它应位于 `cli_agent` package 下，同时保持 `AgentLoop` 和 `EnvironmentKernel` 私有。先建立这一 package seam，可以避免后续工作继续扩展通用的顶层 `runtime` 导入界面。

本工作仅在父 issue 的外部架构门禁通过后开始。它只调整包组织，不增加 Tool Call 或命令执行行为。

### 变更

- 将现有模型类型、Agent Loop 和 Provider 代码移动到 `src/cli_agent/runtime`。
- 更新内部导入、测试和打包配置以使用 `cli_agent.runtime`。
- 保持 `_agent_loop` 等实现模块私有。
- 只导出已经属于公共界面的供应商中立模型类型和官方 Provider Adapter。
- 添加公共导入契约测试，并保持现有纯文本行为不变。
