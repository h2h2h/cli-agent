# feat(runtime): recover from context overflow and report compaction

**状态：** resolved

## 背景

通用 OpenAI-compatible Chat Completions endpoint 没有统一的请求前 Token Count API。
Context Manager 可以使用 Provider-reported `input_tokens`、保守 delta 和安全余量降低
风险，但第一次请求、未知 tokenizer 或供应商计数差异仍可能导致估算偏小。当前
`OpenAICompatibleModelProvider` 对所有非成功 HTTP 响应统一 `raise_for_status()`，
Runtime 无法区分 Context Overflow 与其他网络或鉴权错误。

同时，Tier 1/2/3 的触发和回收是正常 Runtime 行为，Host 需要知道何时发生、释放多少
以及计量来源，但不能在日志或 terminal diagnostic 中泄漏原始消息、命令输出、摘要或
Secret。

[RFC-0010](../../rfcs/approved/RFC-0010-session-context-compaction.md) 将 overflow
恢复定义为水位线估算之外的最后一道保护，并要求恢复过程不能重复已经执行的 Tool。

## 影响

完成后，Provider 可识别的 Context Overflow 会进入一个受限恢复路径：强制压缩并且
只重试原 Model Step 一次，不重复已经执行的 Tool。Host 可以通过稳定的
RuntimeDiagnostic kinds 观察 Snip、Prune、Summarize、oversized guard 和 overflow
恢复，并用统计数据调节水位与安全余量。

## 变更

- 新增 provider-neutral `ModelContextOverflowError`，让 Adapter 保留必要错误分类而不
  暴露供应商完整响应或 Secret-bearing URL。
- 在 OpenAI-compatible Adapter 中识别常见结构化 Context Overflow：
  - 优先读取响应 JSON 中稳定的 error code/type；
  - 只对明确的 context length/max input 错误映射；
  - 鉴权、限流、server error、非法请求和无法识别 payload 保持原错误语义；
  - 错误正文和带 credential 的请求信息不写入 diagnostic。
- 普通 Model Step 捕获 overflow 后：
  - 使当前 usage anchor 失效；
  - 调用 Context Manager 的 force prepare，执行所有可用 Tier 1/2；
  - 存在完整旧 prefix 时允许运行 Tier 3；
  - 再次检查 hard Input Budget；
  - 只重试尚未产生 Completion 的同一个普通 request 一次；
  - 第二次 overflow、摘要失败或无候选时向 Host 返回稳定错误。
- 明确不递归处理摘要请求自身的 overflow；它按 Tier 3 原子失败处理。
- 使用现有 `RuntimeDiagnostic` callback 暴露稳定 kinds：
  - `context.snipped`；
  - `context.pruned`；
  - `context.summarized`；
  - `context.oversized_result`；
  - `context.overflow_recovery`；
  - `context.compaction_failed`。
- Diagnostic detail 只包含 session ID、revision、Tier、usage source、before/after
  tokens、changed entries、summarized turns、summary usage 和触发原因；禁止消息正文、
  Tool 参数/结果、摘要、命令、环境值和 Secret。
- Reference CLI 为上述 normal diagnostics 提供简洁 stderr 展示，同时保持模型输出
  stdout 和现有 completion diagnostics 行为。

## 验收标准

- [ ] 明确 Context Overflow 被映射，其他 HTTP/Provider 错误不被误分类。
- [ ] Overflow 最多重试一次，且发生在 Completion/Tool dispatch 之前。
- [ ] 恢复成功后使用新的 Context Revision，失败后 Session 仍可关闭或重建。
- [ ] 摘要 overflow 不递归调用摘要或无限重试。
- [ ] 每种 Context operation 都有稳定 diagnostic，且测试证明不包含敏感正文。
- [ ] 日志、异常和 CLI 展示不输出 API key 或 Secret-bearing URL。
