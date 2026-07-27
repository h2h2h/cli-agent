# feat(runtime): open and close the host-facing Agent Runtime / 打开和关闭面向宿主的 Agent Runtime

**Status / 状态：** pass
## English

### Background

The project currently exposes internal model and Agent Loop building blocks but has no host-constructed facade. The architecture defines `AgentRuntime` as the only public module that coordinates Workspace-bound resources; hosts must not construct `AgentLoop`, `EnvironmentKernel`, or Environment Bindings themselves.

This change establishes Runtime resource ownership before adding its Session index.

### Changes

- Add the public `AgentRuntime` facade with the architecture-approved asynchronous `open` interface.
- Accept the minimal Workspace and default Model Provider configuration needed by issue 01.
- Open the private Environment Kernel once for the Runtime lifetime.
- Support `async with AgentRuntime.open(...)` and an explicit idempotent `close`.
- Reject new work after Runtime closure with a stable host-facing error.
- Ensure partial open failures release resources already acquired.
- Keep capability reconciliation, Overlay mounting, environment grants, and MCP startup outside this change.
- Add lifecycle tests covering explicit close, context-manager close, repeated close, and failed open cleanup.

## 中文

### 背景

项目目前暴露了内部模型与 Agent Loop 构建块，但没有由宿主构造的 facade。架构将 `AgentRuntime` 定义为协调 Workspace 绑定资源的唯一公共模块；宿主不得自行构造 `AgentLoop`、`EnvironmentKernel` 或 Environment Binding。

本次变更先建立 Runtime 资源所有权，之后再添加 Session 索引。

### 变更

- 添加公共 `AgentRuntime` facade，并提供架构批准的异步 `open` interface。
- 接受 issue 01 所需的最小 Workspace 和默认 Model Provider 配置。
- 在整个 Runtime 生命周期中只打开一次私有 Environment Kernel。
- 支持 `async with AgentRuntime.open(...)` 和显式、幂等的 `close`。
- Runtime 关闭后，以稳定的宿主侧错误拒绝新工作。
- 确保部分打开失败时释放已经获取的资源。
- 不在本次变更中加入能力协调、Overlay 挂载、环境授权和 MCP 启动。
- 添加生命周期测试，覆盖显式关闭、上下文管理器关闭、重复关闭和打开失败清理。
