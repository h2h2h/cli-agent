# feat(provider): add a deterministic scripted model Adapter / 添加确定性的脚本模型 Adapter

**Status / 状态：** pass

## English

### Background

The existing tests define ad hoc providers that return one text completion. Issue 01 needs a reusable Model Provider Adapter that can deterministically drive multiple model requests: first request a Tool Call, then inspect the resulting history and return final assistant text.

The Adapter must exercise the provider-neutral seam directly. It must not use HTTP, provider wire payloads, credentials, timers, or a fake OpenAI transport.

### Changes

- Add `ScriptedModelProvider` as an official in-process implementation of `ModelProvider`.
- Let a test or host configure an ordered script of model event streams, one stream per `generate` call.
- Record every `ModelRequest` received so scenarios can assert messages and fixed built-in tool schemas.
- Fail clearly when the Runtime makes more or fewer model requests than the script defines.
- Preserve asynchronous event-stream behavior without introducing nondeterministic scheduling.
- Replace the one-off greeting provider in Agent Loop tests where the scripted Adapter provides the same coverage.
- Add focused tests for request recording, multi-request scripts, event ordering, and script exhaustion.

## 中文

### 背景

现有测试定义了临时 Provider，并只返回一次文本完成。Issue 01 需要一个可复用的 Model Provider Adapter，以确定性方式驱动多次模型请求：先请求一次 Tool Call，再检查生成的历史并返回最终助手文本。

该 Adapter 必须直接经过供应商中立的 seam，不能使用 HTTP、供应商 wire payload、凭据、计时器或伪造的 OpenAI transport。

### 变更

- 将 `ScriptedModelProvider` 添加为 `ModelProvider` 的官方进程内实现。
- 允许测试或宿主配置有序的模型事件流脚本，每次 `generate` 调用消费一个事件流。
- 记录收到的每个 `ModelRequest`，使场景能够断言消息和固定的内置工具 schema。
- 当 Runtime 发起的模型请求多于或少于脚本定义时，给出清晰失败。
- 保持异步事件流行为，同时不引入非确定性调度。
- 在能够提供同等覆盖的 Agent Loop 测试中，用脚本 Adapter 替换一次性的 greeting provider。
- 添加聚焦测试，覆盖请求记录、多请求脚本、事件顺序和脚本耗尽。
