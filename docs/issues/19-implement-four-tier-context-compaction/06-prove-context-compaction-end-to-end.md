# test(context): prove compaction safety end to end

**状态：** resolved

## 背景

issue 01-05 分别建立预算合同、Context Ledger、Tier 1/2、Tier 3、Overflow 恢复和
观测，但单模块测试不能证明完整 Runtime 顺序：User Message append → request 前
prepare → Provider stream → usage observe → Tool dispatch → Tool Result append →
同一 Turn 的下一次 prepare。Context 压缩还会同时影响 Session 隔离、Prompt payload、
CLI 配置、Host diagnostics 和 close/recreate 生命周期。

[RFC-0010](../../rfcs/approved/RFC-0010-session-context-compaction.md) 的最终验收需要
确定性长程轨迹以及架构/用户文档收口。只有全部行为通过 peer review，才能把对应
issue 标记为 `resolved`，并将 RFC 状态推进到 `COMPLETED`。

## 影响

完成后，四级水位线不再只是独立 reducer 或摘要单测，而是由公共 `AgentRuntime`
和 Reference CLI 场景证明：每个 Model Step 都在请求前管理 Context，Tool 协议始终
合法，压缩失败不会破坏 Session，不同 Session 不泄漏 History，Context Overflow
恢复不会重复副作用。维护者也能从 README 和架构图理解配置、时序和失败边界。

## 变更

- 增加小 Context Window、确定性 token meter 和 scripted provider 的长程测试轨迹：
  - Tier 0 全程不修改 History；
  - 60% 触发 Snip 并回收到 target；
  - 80% 在 Snip 后仍超线才 Prune；
  - 95% 在 Tier 1/2 后仍超线才 Summarize；
  - 初始超过 95% 但 Snip 足够时不调用摘要；
  - hard overflow 的一次恢复与最终失败。
- 覆盖一个 User Turn 内多个 Model Steps：
  - Assistant 发出单个、串行和并行 Tool Calls；
  - Tool Results 追加后、下一次普通 request 前立即 prepare；
  - 最终 Assistant Message 后不立即压缩；
  - 下一次 User Message append 后才重新评估。
- 验证跨层安全合同：
  - System Message 始终为首个且原文不变；
  - Protected Suffix、Active Turn 和最近完整 Turn 保留；
  - Tool Call/Result 的数量、顺序和 `call_id` 集合合法；
  - 摘要投影不提升为 System role；
  - diagnostic 不包含用户文本、Tool output、命令或 Secret。
- 扩展 Session 集成：
  - 同 Session 并发 turn 串行化；
  - 不同 Session 同时触发不同 Tier 仍完全隔离；
  - close_session 释放 Context；复用相同 ID 创建全新 revision、summary 和 usage；
  - Runtime close 期间的 pending summary/Provider stream 能正确取消和清理。
- 增加三类可重复评估 fixture：
  - 多次文件探索和局部读取；
  - 长命令输出和重复 `output` 轮询；
  - 多轮修改、测试失败、修复和最终验证；
  记录每步 input tokens、Provider/summary 调用次数、保留事实和最终答案完整性。
- 更新用户与架构文档：
  - `.envrc.example`、README 和 CLI 配置说明 Context Window、输出预留和安全余量；
  - `docs/architecture.md` 增加 Session Context Manager、request 前 prepare、response
    后 observe、四级水位线和无 Tools summarizer；
  - 说明 Reported/Estimated usage、Prompt Cache 取舍、overflow 单次恢复和非目标；
  - 全部子 issue 通过 peer review 后，将 RFC-0010 状态更新为 `COMPLETED`。
- 运行完整 pytest、Ruff、mypy 和 diff check，记录结果供同行评审；不依赖 live
  Provider 才能完成 required test suite。

## 验收标准

- [ ] 四级边界、累积执行、hysteresis、minimum reclaim 和 oversized guard 均由
      公共 Runtime 场景覆盖。
- [ ] 同一 User Turn 的每个 Model Step 都在 request 前 prepare，Turn 结束不预压缩。
- [ ] 所有压缩与失败路径保持 Tool 协议、Session 隔离和 close lifecycle 正确。
- [ ] Overflow 恢复不重复 Tool execution，第二次失败稳定终止。
- [ ] README、`.envrc.example`、architecture 和 RFC 状态与最终实现一致。
- [ ] 完整 pytest、Ruff、mypy 和 diff check 通过后才申请 peer review。
