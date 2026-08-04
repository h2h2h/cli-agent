# feat(context): snip and prune stale tool results

**状态：** resolved

## 背景

issue 01-02 建立预算、Context Ledger 和请求前 prepare 后，History 仍然不会释放空间。
当前 `exec`、`output` 和 `kill` 返回的 Execution snapshot 最多包含 200 个输出块，
Session 内 Execution State 又会保留其输出供后续按 Cursor 重读。旧的成功 Tool Result
因此是体积大、恢复路径明确且无需 LLM 即可处理的首要压缩对象。

[RFC-0010](../../rfcs/approved/RFC-0010-session-context-compaction.md) 要求 Tier 1/2
只处理 Protected Suffix 之外的 Tool Result，并保持 Assistant Tool Call、Tool Result
Message 和 `call_id` 完整。普通 User/Assistant 文本、错误结果和未知 payload 不在本
issue 范围内。

## 影响

完成后，Context Pressure 达到 60% 时 Runtime 可以用确定性 Snip 回收旧输出，达到
80% 且 Snip 不足时可进一步 Prune 已截短结果；两个 Tier 均不调用模型。模型仍能知道
调用过什么、执行是否成功、如何定位 Execution，并在 Session 内需要时重新读取输出。

## 变更

- 实现 Protected Suffix 选择：
  - 从 History 末尾累计策略指定的 Token 预算；
  - 向外扩展到完整 User Turn 边界；
  - 始终包含 Active Turn 和最近一个完整 Turn；
  - 普通 Tier 1/2 不修改保护区内结果。
- 为当前 Execution snapshot 实现 schema-aware `_ToolResultReducer`：
  - 只接受结构可识别、成功且具有 `exec_id` 恢复语义的结果；
  - 保留 `call_id`、Tool 名、`exec_id`、status、exit code、Cursor、terminal 与
    truncated facts；
  - Snip 保留 UTF-8 安全的有界 head/tail，并记录省略的 chunk/byte 数与重读提示；
  - Prune 只保留执行识别、最终状态、压缩标记和可行的重读方法；
  - 不对任意 JSON stringify 后按字符截断。
- 为每个候选建立不可逆状态：`raw -> snipped -> pruned`；重复 prepare 不再次修改
  同一状态，也不保留用于恢复 prompt 原文的大 payload。
- 实现累积水位编排：
  - pressure 达到 60% 时从最旧 raw 候选执行 Snip，并向 Snip target 回收；
  - 重新测量仍达到 80% 时先完成适用 Snip，再从最旧 snipped 候选执行 Prune；
  - 达到 target、无候选或回收不足 minimum reclaim 时停止；
  - 初始 pressure 超过 80% 也必须先运行 Tier 1，不能跳级。
- 实现 excluded tools 和默认跳过规则：
  - error Tool Result、未知 payload、缺少恢复语义的结果不压缩；
  - 新 syscall 默认不可压缩，必须显式增加 reducer 规则；
  - 不解析 User Markdown，不截断普通 Assistant Message。
- 增加 oversized success Tool Result guard：
  - 单个最新结果会使下一次请求无法进入 Input Budget 时，即使位于 Active Turn，
    也允许先 Snip 可重读 payload；
  - oversized User Message、Tool Call 参数或不可恢复结果返回明确错误，不静默删除。
- 增加操作统计，记录 Tier、revision、before/after token、候选数和回收量，但不记录
  User/Assistant/Tool payload 正文。

## 验收标准

- [ ] Tier 1/2 的 Provider 与摘要模型调用次数始终为零。
- [ ] 压缩前后 Tool Call/Result Message 数量、顺序和 `call_id` 集合完全一致。
- [ ] stdout、stderr、混合 stream、无换行长块和多字节 UTF-8 均安全处理。
- [ ] Protected、error、excluded 和未知 Tool Result 保持不变。
- [ ] 状态转换单调、重复 prepare 幂等，minimum reclaim 和回收目标可测试。
- [ ] oversized 可重读结果能降级，无法安全降级的当前输入 fail closed。

