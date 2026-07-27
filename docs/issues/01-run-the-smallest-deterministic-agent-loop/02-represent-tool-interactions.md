# feat(model): represent provider-neutral Tool interactions / 表示供应商中立的工具交互

**Status / 状态：** pass

## English

### Background

The current model contains only text blocks, user messages, assistant messages, text deltas, and terminal completions. An Agent Loop cannot commit a model Tool Call or return a Tool Result without provider-neutral, serializable conversation types.

These types belong at the Model Provider seam. They must not contain OpenAI payload dictionaries or Environment Kernel objects.

### Changes

- Add a provider-neutral representation for a complete Tool Call with its call ID, name, and decoded arguments.
- Add a provider-neutral Tool Result associated with the originating call ID, including successful and failed results.
- Allow model messages to retain ordered text and Tool Call content without losing model-returned order.
- Extend `ModelEvent` so a provider can yield a fully assembled Tool Call before terminal completion metadata.
- Ensure Tool Calls and Tool Results can be retained in Conversation History and included in a later `ModelRequest`.
- Add model tests covering equality, ordering, and a Tool Call followed by its Tool Result.

## 中文

### 背景

当前模型只包含文本块、用户消息、助手消息、文本增量和终止完成事件。缺少供应商中立且可序列化的对话类型时，Agent Loop 无法提交模型 Tool Call，也无法返回 Tool Result。

这些类型属于 Model Provider seam，不得包含 OpenAI payload 字典或 Environment Kernel 对象。

### 变更

- 为完整 Tool Call 添加供应商中立的表示，包含调用 ID、名称和已解码参数。
- 添加与原始调用 ID 关联的供应商中立 Tool Result，同时支持成功和失败结果。
- 允许模型消息按原始顺序保留文本与 Tool Call 内容，不丢失模型返回顺序。
- 扩展 `ModelEvent`，使 Provider 能在终止完成元数据之前产出已完整组装的 Tool Call。
- 确保 Tool Call 和 Tool Result 能保留在 Conversation History 中，并进入后续 `ModelRequest`。
- 添加模型测试，覆盖相等性、顺序以及 Tool Call 后接 Tool Result 的场景。
