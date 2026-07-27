# feat(provider): encode complete OpenAI-compatible chat history / 编码完整的 OpenAI-compatible 对话历史

**Status / 状态：** pass

## English

### Background

The current OpenAI-compatible Adapter serializes only text-only User and Assistant Messages. It cannot send Runtime-assembled System Messages, Assistant Tool Calls, or Tool Results, so the real Provider cannot participate in the Agent loop already proven with the scripted Provider.

This issue completes message conversion only. Advertising built-in tools and decoding streamed Tool Calls remain separate changes.

### Changes

- Convert provider-neutral System, User, Assistant, and Tool Result Messages to the supported Chat Completions wire roles.
- Serialize Assistant text and ordered Tool Calls, including call IDs, function names, and JSON-encoded arguments.
- Expand one `ToolResultMessage` into ordered wire tool messages correlated by `tool_call_id`.
- Serialize either the successful output or structured error as JSON Tool Result content.
- Preserve Conversation History order without adding OpenAI dictionaries to model types or AgentLoop.
- Add deterministic `httpx.MockTransport` request tests covering a complete Tool Call followed by multiple Tool Results.

## 中文

### 背景

当前 OpenAI-compatible Adapter 只能序列化纯文本 User 和 Assistant Message。它无法发送由 Runtime 组装的 System Message、Assistant Tool Call 或 Tool Result，因此真实 Provider 还不能参与已经由 scripted Provider 验证过的 Agent loop。

本 issue 只完成 Message 转换；公布 built-in tools 和解码流式 Tool Call 由后续变更负责。

### 变更

- 将供应商中立的 System、User、Assistant 和 Tool Result Message 转换为受支持的 Chat Completions wire role。
- 序列化 Assistant 文本和有序 Tool Call，包括 call ID、function 名称及 JSON 编码参数。
- 将一个 `ToolResultMessage` 展开为按顺序排列、通过 `tool_call_id` 关联的 wire tool message。
- 将成功 output 或结构化 error 序列化为 JSON Tool Result 内容。
- 保留 Conversation History 顺序，不向模型类型或 AgentLoop 添加 OpenAI 字典。
- 使用确定性的 `httpx.MockTransport` 添加请求测试，覆盖完整 Tool Call 后跟多个 Tool Result。
