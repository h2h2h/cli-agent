# feat(model): represent provider-neutral model usage / 表示供应商中立的模型用量

**Status / 状态：** pass

## English

### Background

`ModelCompletion` currently retains only the Assistant Message and completion reason. A real Provider also reports token usage, but placing OpenAI fields such as `prompt_tokens` directly on the completion would make the host-facing event contract provider-specific.

The Runtime needs one small common usage shape before the Adapter can decode streaming usage metadata.

### Changes

- Add an immutable `ModelUsage` with non-negative input, output, and total token counts.
- Add optional usage to `ModelCompletion`; use `None` when a Provider does not report it.
- Keep cached-token, reasoning-token, billing, and raw provider metadata outside the common subset.
- Export `ModelUsage` through the public `cli_agent.runtime` package.
- Update existing completion construction without changing text or Tool Call event behavior.
- Add tests covering populated and absent usage, equality, and public importability.

## 中文

### 背景

`ModelCompletion` 当前只保留 Assistant Message 和 completion reason。真实 Provider 还会返回 token 用量，但如果直接把 OpenAI 的 `prompt_tokens` 等字段放进 completion，面向宿主的事件契约就会与供应商绑定。

在 Adapter 解码流式 usage metadata 之前，Runtime 需要一个小而通用的用量形状。

### 变更

- 添加不可变的 `ModelUsage`，包含非负的输入、输出和总 token 数。
- 为 `ModelCompletion` 添加可选 usage；Provider 未返回用量时使用 `None`。
- 不把 cached token、reasoning token、计费信息和原始供应商 metadata 放入公共子集。
- 通过公共 `cli_agent.runtime` package 导出 `ModelUsage`。
- 更新现有 completion 构造，同时不改变文本或 Tool Call 事件行为。
- 添加测试，覆盖有 usage、无 usage、相等性和公共导入。
