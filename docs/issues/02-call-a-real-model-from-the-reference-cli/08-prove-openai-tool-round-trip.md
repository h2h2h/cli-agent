# test(provider): prove an OpenAI-compatible Tool round trip / 验证 OpenAI-compatible Tool 往返

**Status / 状态：** pass

## English

### Background

Focused request and stream tests prove individual Adapter conversions, but the parent issue also needs evidence that the real HTTP Adapter can drive the public Runtime loop: request a command, receive its Tool Result, and complete the turn.

The test must remain deterministic and require neither network access nor a live credential.

### Changes

- Add an integration test using `AgentRuntime`, `OpenAICompatibleModelProvider`, and `httpx.MockTransport`.
- Return a fragmented `exec` Tool Call from the first fake streaming response.
- Assert that the first HTTP request begins with the Runtime-assembled System Message followed by the User task.
- Execute a short command in the test Workspace and capture the second HTTP request.
- Assert that the second request contains the matching Assistant Tool Call and JSON Tool Result.
- Return final streamed text, completion reason, and usage from the second fake response.
- Assert that provider-neutral events and Conversation History complete through the public Runtime API.
- Block external network access and use only a placeholder credential.

## 中文

### 背景

聚焦的请求和 stream 测试可以验证各个 Adapter 转换，但父 issue 还需要证明真实 HTTP Adapter 能驱动公共 Runtime loop：请求执行命令、接收 Tool Result，并完成整个 turn。

该测试必须保持确定性，既不访问网络，也不需要真实凭据。

### 变更

- 使用 `AgentRuntime`、`OpenAICompatibleModelProvider` 和 `httpx.MockTransport` 添加集成测试。
- 在第一次 fake 流式响应中返回一个分片的 `exec` Tool Call。
- 断言第一次 HTTP 请求以 Runtime 组装的 System Message 开始，随后是 User task。
- 在测试 Workspace 中执行短命令，并捕获第二次 HTTP 请求。
- 断言第二次请求包含匹配的 Assistant Tool Call 和 JSON Tool Result。
- 在第二次 fake 响应中返回最终流式文本、completion reason 和 usage。
- 断言供应商中立事件与 Conversation History 通过公共 Runtime API 完整结束。
- 阻止外部网络访问，并只使用占位凭据。
