# feat(runtime): introduce host-owned user interaction

**状态：** resolved

## 背景

现有 `ExecutionApprover` 只服务于 Policy 的 `ASK`。但模型之外的 Runtime 将来还可能
需要让用户从多个选项中决策或提供自由文本。继续增加场景专用 callback 会重复 Host
集成、取消和生命周期逻辑，也会把 Policy 与具体 UI 绑定。

RFC-0008 将提问能力定义为始终存在的 Host 能力；Policy 只产生 ASK 结论，由 Kernel
转换为标准问题。

## 影响

完成后，终端、GUI 或远端 Host 都能通过同一个最小接口回答 Runtime-owned 问题。
Policy 不需要了解 UI，Runtime 也不拥有或关闭 Host 的交互对象。模型可见 syscall
仍只有 `exec`、`output` 和 `kill`。

## 变更

- 新增不可变的 `UserOption(value, label)`、
  `UserQuestion(request_id, session_id, prompt, options)` 和
  `UserAnswer(value)`。
- 定义 `UserInteraction.ask(UserQuestion) -> UserAnswer` Protocol。
- 规定空 options 接受自由文本；非空 options 只接受已声明 value；`None` 表示取消或
  无法回答。
- 将 `user_interaction` 设为 `AgentRuntime.open` 必选参数，即使 Policy 为 `None` 也
  必须提供。
- 由 Host 创建和拥有 Runtime-wide interaction；Runtime 与 Session close 只取消
  pending ask，不关闭 interaction。
- 将 Policy ASK 转为包含 reason 与原始 command 的标准问题，固定提供
  `allow_once` 和 `deny`。
- 只允许 `allow_once` 放行当前命令；`deny`、取消、异常和非法回答均以
  `policy_denied` 阻止当前命令。
- 交互异常与非法回答写入 Host diagnostic，当前 Session 保持可用。
- Reference CLI 注册终端交互实现（`_TerminalUserInteraction` 取代
  `_TerminalExecutionApprover`）。
- 删除 `ExecutionApprover`、`ExecutionApprovalRequest`、`ApprovalResponse` 和
  `_ExecutionApprovalGate`，不保留兼容层。
- `EnvironmentKernel` 的 `approval_gate`/`approval_session_id` 参数改为
  `user_interaction`/`session_id`；未配置 interaction 时 ASK fail closed。
- 不增加 Runtime 固定 timeout，不增加模型 question syscall，不实现多 Session
  并发提问队列。

## 验收标准

- [x] Runtime open 缺少 `user_interaction` 时不能静默创建默认对象。
- [x] ASK 的标准问题只暴露本次决策所需的 reason、command 和两个固定选项。
- [x] `allow_once` 不会持久化或影响下一条命令。
- [x] cancel、异常和非法值 fail closed，且不会终止 Session。
- [x] Session/Runtime close 能取消 pending ask，但不会调用 interaction close。
- [x] 公共 API 和 Reference CLI 中不再出现专用 Approver 类型。
