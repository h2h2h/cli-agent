# feat(provider): assemble streamed OpenAI Tool Calls / 组装流式 OpenAI Tool Call

**Status / 状态：** pass

## English

### Background

Chat Completions streams a Tool Call across multiple deltas: its ID, function name, and JSON arguments may arrive separately, and multiple calls are correlated by index. The current Adapter reads only text deltas and therefore loses every real model Tool Call.

The Adapter must absorb this fragmented wire behavior and emit only complete provider-neutral calls.

### Changes

- Accumulate streamed Tool Call fragments by their wire index.
- Assemble each call ID, function name, and argument string without exposing fragment objects outside the Adapter.
- Decode complete arguments into the provider-neutral JSON object required by `ToolCall`.
- Emit one `ToolCallReady` per completed call in model-returned order.
- Build the terminal `AssistantMessage` from accumulated text and ordered complete Tool Calls.
- Fail clearly on missing identity fields or invalid final argument JSON without emitting a partial Tool Call.
- Add fake SSE tests covering fragmented arguments, multiple calls, text plus calls, and stable call ordering.

## 中文

### 背景

Chat Completions 会把一个 Tool Call 拆成多个 delta：ID、function 名称和 JSON 参数可能分别到达，多个调用则通过 index 关联。当前 Adapter 只读取文本 delta，因此会丢失真实模型产生的所有 Tool Call。

Adapter 必须吸收这种分片 wire 行为，对外只产出完整的供应商中立调用。

### 变更

- 按 wire index 累积流式 Tool Call 分片。
- 组装每个调用的 ID、function 名称和参数字符串，不向 Adapter 外暴露 fragment 对象。
- 将完整参数解码为 `ToolCall` 要求的供应商中立 JSON object。
- 按模型返回顺序为每个完整调用产出一个 `ToolCallReady`。
- 使用累积文本和有序完整 Tool Call 构造终止 `AssistantMessage`。
- 当身份字段缺失或最终参数 JSON 无效时清晰失败，且不产出部分 Tool Call。
- 添加 fake SSE 测试，覆盖参数分片、多个调用、文本加调用以及稳定调用顺序。
