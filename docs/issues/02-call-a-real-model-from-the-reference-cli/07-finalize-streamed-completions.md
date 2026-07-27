# feat(provider): finalize streamed completions with usage / 使用 usage 完成流式 completion

**Status / 状态：** pass

## English

### Background

The Adapter currently returns as soon as it sees a finish reason. In the supported Chat Completions stream, usage may arrive in a later chunk with an empty choices list, before the terminal marker. Returning early discards that metadata and can produce a completion before the response is actually finished.

The Adapter needs one terminalization path shared by text-only and Tool Call responses.

### Changes

- Request streaming usage through the supported Chat Completions option.
- Retain the final completion reason while continuing to consume usage and the terminal stream marker.
- Translate common prompt, completion, and total token counts into `ModelUsage`.
- Yield exactly one `ModelCompletion` after all supported terminal metadata has been collected.
- Use `usage=None` when a compatible endpoint omits usage.
- Preserve all preceding `TextDelta` and `ToolCallReady` events and the assembled Assistant Message.
- Add fake SSE tests for usage in an empty-choices chunk, absent usage, text completion, and Tool Call completion.

## 中文

### 背景

当前 Adapter 一看到 finish reason 就会返回。在受支持的 Chat Completions stream 中，usage 可能在之后以 choices 为空的 chunk 到达，并位于终止标记之前。过早返回会丢失 metadata，也可能在响应真正结束前产出 completion。

Adapter 需要一条由纯文本响应和 Tool Call 响应共用的终止路径。

### 变更

- 通过受支持的 Chat Completions 选项请求流式 usage。
- 保存最终 completion reason，同时继续消费 usage 和终止流标记。
- 将通用的 prompt、completion 和 total token 数转换为 `ModelUsage`。
- 收集完所有受支持的终止 metadata 后，只产出一个 `ModelCompletion`。
- compatible endpoint 未返回 usage 时使用 `usage=None`。
- 保留之前的全部 `TextDelta`、`ToolCallReady` 事件和已组装 Assistant Message。
- 添加 fake SSE 测试，覆盖 choices 为空的 usage chunk、缺少 usage、文本 completion 和 Tool Call completion。
