# feat(agent-loop): dispatch model Tool Calls in returned order / 按模型返回顺序派发 Tool Call

**Status / 状态：** pass

## English

### Background

A model may return multiple complete Tool Calls in one turn. The parent issue requires the Runtime to dispatch them in model-returned order. Correct behavior must not depend on asynchronous iteration timing or parallel task scheduling.

This change extends the single-Tool-Call loop without adding Session execution queues or parallel execution policy.

### Changes

- Retain all complete Tool Calls from one assistant turn in their original content order.
- Dispatch those calls serially through the same Environment Binding.
- Append Tool Results to Conversation History in the same order as their Tool Calls.
- Do not start a later call before the preceding call has returned its Tool Result.
- Submit one follow-up model request after the ordered batch has completed.
- Add a deterministic test whose second Tool Call observes an effect from the first.
- Add a regression test proving that event streaming does not reorder dispatch.

## 中文

### 背景

模型可能在一个 turn 中返回多个完整 Tool Call。父 issue 要求 Runtime 按模型返回顺序派发它们。正确行为不能依赖异步迭代时序或并行任务调度。

本次变更扩展单 Tool Call 循环，但不增加 Session Execution 队列或并行执行策略。

### 变更

- 按原始内容顺序保留一个助手 turn 中的所有完整 Tool Call。
- 通过同一个 Environment Binding 串行派发这些调用。
- 按 Tool Call 相同顺序将 Tool Result 追加到 Conversation History。
- 前一个调用返回 Tool Result 之前，不启动后一个调用。
- 有序批次全部完成后，只提交一次后续模型请求。
- 添加确定性测试，使第二个 Tool Call 能观察到第一个调用产生的效果。
- 添加回归测试，证明事件流不会改变派发顺序。
