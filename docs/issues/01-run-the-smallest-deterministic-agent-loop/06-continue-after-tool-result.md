# feat(agent-loop): continue generation after a Tool Result / 在 Tool Result 后继续生成

**Status / 状态：** pass

## English

### Background

The current Agent Loop makes one model request and stops at the first completion. With Tool interaction types, fixed schemas, a scripted Provider, and an Environment Binding available, it can implement the smallest complete Agent turn without learning command or provider-specific details.

The Agent Loop should depend only on the Model Provider interface and its Session-scoped Environment Binding. Environment Kernel internals must remain behind that seam.

### Changes

- Inject an Environment Binding into each Agent Loop.
- Include the canonical `exec`, `output`, and `kill` schemas in every `ModelRequest`.
- Collect the complete assistant Tool Call message for a successful model attempt.
- Dispatch the Tool Call through the Environment Binding.
- Append the assistant Tool Call and corresponding Tool Result to Conversation History.
- Submit the updated history to the same Session-bound Model Provider.
- Continue until a terminal assistant response contains no Tool Call, then commit and yield the final completion.
- Add an Agent Loop test for `User Message → exec Tool Call → Tool Result → final Assistant Message`.

## 中文

### 背景

当前 Agent Loop 只发起一次模型请求，并在第一次完成时停止。在具备 Tool 交互类型、固定 schema、脚本 Provider 和 Environment Binding 后，它可以实现最小完整 Agent turn，而无需了解命令或供应商专用细节。

Agent Loop 应仅依赖 Model Provider interface 及其 Session 作用域的 Environment Binding。Environment Kernel 内部实现必须保持在该 seam 之后。

### 变更

- 向每个 Agent Loop 注入 Environment Binding。
- 在每个 `ModelRequest` 中包含规范的 `exec`、`output`、`kill` schema。
- 为一次成功模型尝试收集完整的助手 Tool Call 消息。
- 通过 Environment Binding 派发 Tool Call。
- 将助手 Tool Call 及对应 Tool Result 追加到 Conversation History。
- 将更新后的历史提交给同一个 Session 绑定的 Model Provider。
- 持续运行，直到终止助手响应不含 Tool Call，然后提交并产出最终完成事件。
- 添加 Agent Loop 测试，覆盖 `User Message → exec Tool Call → Tool Result → final Assistant Message`。
