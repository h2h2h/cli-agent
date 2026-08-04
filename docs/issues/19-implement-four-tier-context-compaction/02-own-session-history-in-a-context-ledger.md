# feat(context): own session history in a context ledger

**状态：** resolved

## 背景

[RFC-0010](../../rfcs/approved/RFC-0010-session-context-compaction.md) 选择
Session-scoped Context Manager 作为 Conversation History 的唯一所有者。当前
`AgentLoop` 直接维护只追加的 `_history`，并在每个 Model Step 中读取完整 tuple；
usage 没有绑定到产生它的请求版本，同一 Session 的并发 `run_turn` 也没有单写者
边界。

Tier 1/2/3 都依赖正确识别 User Turn、Active Turn 和 Tool Exchange。如果先在现有
list 上加入字符串压缩，后续再迁移所有权，会同时维护两套 History 和边界语义，难以
证明请求序列没有变化。因此本 issue 先建立不执行实际压缩的 Context Ledger 和调用
时序。

## 影响

完成后，Agent Loop 只负责 Append、Prepare、Generate、Observe 和 Tool dispatch
编排；所有普通 Model Request 都由 Context Manager 产生。每个 Provider usage 会与
不可变 Context Revision 对应，Turn 与并行 Tool Call 配对可以脱离网络独立测试。
在尚未加入压缩操作时，合法任务的模型请求序列应与当前行为等价。

## 变更

- 新增 Session-scoped `_ContextManager` 和 `_ContextLedger`，由它们保存：
  - 初始 System Message；
  - 有序 User、Assistant 和 Tool Result Messages；
  - 当前 Context Revision；
  - User Turn、Active Turn 和完整 Tool Exchange 边界；
  - 最近一次 Provider-reported usage anchor 及其 request revision。
- 定义最小接口：
  - `append(message)` 是 History 唯一写入口；
  - `prepare_request()` 返回不可变 request、revision 和 Context Pressure；
  - `observe(revision, usage)` 只更新对应已发送 request 的观测；
  - History/debug projection 从 Context Manager 读取，不再暴露可变 list。
- 将现有 Agent Loop 时序迁移为：
  - User Message append 后、每次普通 `Provider.generate` 前 prepare；
  - 收到完整 `ModelCompletion` 后先 observe，再 append Assistant Message；
  - 有 Tool Call 时 dispatch 并 append 完整 Tool Result Message；
  - 下一次 Model Step 再 prepare；最终 Assistant Message 后不主动压缩。
- 从 Assistant Message 和 Tool Result Message 验证 Tool Exchange：
  - 并行 Tool Call 按 Assistant Message 中的完整 `call_id` 集合匹配；
  - ToolCallReady 的到达顺序不改变配对或 dispatch 顺序；
  - 缺失、多余、重复和跨 Turn `call_id` 产生稳定内部错误，不删除消息自愈。
- 使用上一请求 `usage.input_tokens` 作为 Reported anchor；对无 usage 和 revision
  之后的新增内容标记为 Estimated，不把 `total_tokens` 直接当作下一次输入水位。
- 为每个 `_Session` 增加一个完整 User Turn 的串行 lock：
  - 同一 `session_id` 的并发 `run_turn` 按进入顺序串行；
  - 不同 Session 继续并发；
  - close 会正确取消/等待当前 Session 工作，不共享 Context 状态。
- 迁移现有 Agent Loop、Runtime、多 Session 和 scripted provider tests；删除
  `_history` 的双写或兼容路径。

## 验收标准

- [ ] 每个普通 Model Request 都由 Context Manager 的 prepare 产生。
- [ ] 一个含多次 Tool Call 的 User Turn 会在每个 Model Step 前 prepare，而非只在
      Turn 结束后处理。
- [ ] 无压缩操作时，现有单轮、多轮和 Tool round-trip 请求序列保持等价。
- [ ] 并行 Tool Calls 的 `call_id` 配对不可被 ready event 顺序或 cutoff 拆分。
- [ ] 同 Session 单写、跨 Session 并发和 close/recreate 均有确定性测试。

