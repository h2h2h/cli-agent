# feat(cli): run and present one Agent turn / 运行并展示一个 Agent turn

**Status / 状态：** pass

## English

### Background

The configured cli-agent still needs to open the public Runtime, submit the user's task, and present its provider-neutral event stream. All model interaction and command execution must continue through `AgentRuntime`; the command-line entry point must not dispatch built-in tools itself.

This issue implements one completed task, not an interactive shell or persistent Session store.

### Changes

- Construct `OpenAICompatibleModelProvider` from validated CLI configuration.
- Open `AgentRuntime`, run the task in one CLI-owned Session, and close the Session and Runtime through public lifecycle methods.
- Rely on the Runtime to assemble and inject the Session's System Message; do not construct or prepend it in the CLI.
- Write `TextDelta` content to standard output as it arrives without duplicating the final Assistant text.
- Present `ToolCallReady`, completion reason, and available usage as concise diagnostics without exposing provider wire objects.
- Return a successful process status only after a terminal `ModelCompletion`.
- Keep all built-in tool dispatch, Conversation History, and Workspace command behavior inside the Runtime.
- Add CLI runner and renderer tests using provider-neutral scripted events, with no HTTP or real credential.

## 中文

### 背景

完成配置的 cli-agent 还需要打开公共 Runtime、提交用户任务，并展示供应商中立事件流。所有模型交互和命令执行都必须继续经过 `AgentRuntime`；命令行入口不能自行派发内置工具。

本 issue 实现一次完整任务，而不是交互式 shell 或持久化 Session store。

### 变更

- 使用已验证的 CLI 配置构造 `OpenAICompatibleModelProvider`。
- 打开 `AgentRuntime`，在一个由 CLI 持有的 Session 中运行任务，并通过公共生命周期方法关闭 Session 和 Runtime。
- 由 Runtime 组装并注入 Session 的 System Message；CLI 不自行构造或前置该消息。
- 在 `TextDelta` 到达时写入标准输出，且不重复输出最终 Assistant 文本。
- 将 `ToolCallReady`、completion reason 和可用 usage 展示为简洁诊断，不暴露供应商 wire object。
- 只有收到终止 `ModelCompletion` 后才返回成功进程状态。
- 将所有内置工具派发、Conversation History 和 Workspace 命令行为保留在 Runtime 内。
- 使用供应商中立 scripted event 添加 CLI runner 和 renderer 测试，不访问 HTTP，也不使用真实凭据。
