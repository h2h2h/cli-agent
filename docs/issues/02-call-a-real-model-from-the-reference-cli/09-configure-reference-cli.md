# feat(cli): configure the thin Reference CLI / 配置轻量 Reference CLI

**Status / 状态：** pass

## English

### Background

The Runtime can be embedded from Python but the project has no executable Reference CLI. Before it can run a task, the CLI needs a small, explicit configuration boundary for the Workspace and OpenAI-compatible Provider.

The CLI is a host example, not a second Runtime. It must not construct private modules or define behavior unavailable through `AgentRuntime`.

### Changes

- Add a `cli-agent` console entry point and a small Reference CLI module.
- Accept one task plus an optional Workspace override.
- Read model, base URL, and API key from `CLI_AGENT_MODEL`, `CLI_AGENT_BASE_URL`, and `CLI_AGENT_API_KEY`.
- Make direnv the primary documented way to load Provider environment variables, while keeping it a host-side tool rather than a Runtime dependency.
- Ignore the user's real `.envrc` and provide a safe `.envrc.example` containing placeholder values.
- Normalize and validate configuration before opening the Runtime.
- Produce concise configuration errors without logging credentials.
- Keep parsing and Provider construction separate from event presentation so both can be tested deterministically.
- Add parser and configuration tests covering defaults, overrides, missing credentials, invalid Workspace, and secret non-disclosure.

## 中文

### 背景

当前 Runtime 可以由 Python 宿主嵌入，但项目还没有可执行的 Reference CLI。在运行任务之前，CLI 需要一个小而明确的配置边界，用于设置 Workspace 和 OpenAI-compatible Provider。

CLI 是宿主示例，而不是第二套 Runtime。它不能构造私有模块，也不能定义 `AgentRuntime` 无法提供的行为。

### 变更

- 添加 `cli-agent` console entry point 和小型 Reference CLI module。
- 接受一个任务，以及可选的 Workspace 覆盖参数。
- 从 `CLI_AGENT_MODEL`、`CLI_AGENT_BASE_URL` 和 `CLI_AGENT_API_KEY` 读取 model、base URL 和 API key。
- 将 direnv 作为加载 Provider 环境变量的主要文档化方案，同时保持它是宿主侧工具，而不是 Runtime 依赖。
- 忽略用户实际使用的 `.envrc`，并提供只包含占位值的安全 `.envrc.example`。
- 在打开 Runtime 前规范化并验证配置。
- 输出简洁的配置错误，且不记录凭据。
- 将参数解析和 Provider 构造与事件展示分开，以便确定性测试。
- 添加 parser 和配置测试，覆盖默认值、覆盖值、缺失凭据、无效 Workspace 和 secret 不泄漏。
