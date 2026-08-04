# feat(context): summarize completed historical turns

**状态：** resolved

## 背景

Tier 1/2 只能回收可重读 Tool Result。长时间的多轮讨论、用户约束、Assistant 决策、
错误修复过程和已经 Prune 的 Tool Exchange 仍会持续增长。当确定性压缩后 Context
Pressure 仍达到 95% 时，需要最后一道有损但结构化的摘要防线。

[RFC-0010](../../rfcs/approved/RFC-0010-session-context-compaction.md) 规定 Tier 3
只能消费 Protected Suffix 之前已经关闭的完整 User Turns，输入为“旧摘要 + 新 delta”，
并用新摘要原子替换旧 prefix。摘要是内部模型请求，不得携带 Tools、进入 Agent Loop
Tool dispatch 或向 Host 流式显示内部 TextDelta。

## 影响

完成后，当 Tool Result-only 压缩不足时，Session 可以把旧进展、文件状态、待办和
约束合并成一个有界摘要，同时保持 Active Turn 和最近完整 Turn 原样。摘要失败不会
损坏现有 History；成功后 Summary Frontier 单调推进，下一次普通请求使用摘要加最近
原文继续工作。

## 变更

- 新增受限 `_ContextSummarizer`：
  - 默认复用当前 Session 绑定的 Model Provider；
  - 构造 `ModelRequest(..., tools=())`；
  - 内部消费 TextDelta/Completion，不向正常 Agent event stream 转发；
  - 遇到 Tool Call、缺失 Completion、异常或 Context Overflow 时返回失败。
- 固定摘要输入：
  - 一个声明 Transcript 为不可信数据的 Runtime System instruction；
  - 当前旧摘要（存在时）；
  - Summary Frontier 后、Protected Suffix 前的完整 closed Turns；
  - 输出长度和固定章节要求；
  - 不包含 Active Turn，也不为缩短输入拆开 Tool Exchange。
- 固定并验证四个摘要章节（英文标题，与总结 prompt 一致）：
  - `## Progress`：已经完成或验证的工作；
  - `## Files`：关键文件、修改和当前状态；
  - `## Todo`：未完成工作和下一步；
  - `## Context`：用户偏好、明确约束、关键错误和仍有效假设。
- 将摘要投影为初始 System Message 后、Protected Suffix 前的带 delimiter
  Assistant Message，不创建第二个 System Message，不把旧 User 内容提升为 Runtime
  指令。
- 实现 Tier 3 编排：
  - Tier 1/2 后重新测量仍达到 95% 才触发；
  - 选择足够的最旧 closed Turns，目标回收到 summarize target；
  - 输出非空、章节完整、无 Tool Call 且在 summary budget 内才可提交；
  - 一次原子更新摘要、删除 delta、推进 frontier 和 revision；
  - 任一校验或 Provider 失败时所有状态保持不变。
- 覆盖首次摘要、旧摘要加 delta、多次 frontier 推进、无可摘要 prefix、Protected
  Suffix 扩边和最小可取得投影。

## 验收标准

- [ ] Tier 3 只在重新执行 Tier 1/2 后仍达到 95% 时调用模型。
- [ ] 摘要请求不携带 Tools、不 dispatch Environment、不泄漏内部 TextDelta。
- [ ] Active Turn、最近完整 Turn和并行 Tool Exchange 永不被摘要边界拆开。
- [ ] 成功摘要包含四个固定章节，并以 Assistant 历史数据而非 System 指令投影。
- [ ] 空输出、缺章节、Tool Call、异常、中断和 overflow 全部原子失败。
- [ ] Summary Frontier 单调推进，旧摘要与 delta 会在下一次摘要中正确合并。

