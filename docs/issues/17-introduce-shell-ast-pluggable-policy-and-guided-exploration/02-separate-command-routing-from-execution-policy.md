# refactor(runtime): separate command routing from execution policy

**状态：** resolved

## 背景

现有执行链通过 `PolicyEvaluation` 和 `ExecutionDecision` 间接传递 parse result，Router
必须等待授权对象才能取得命令。这把语法、授权与执行选择绑定在同一对象链上，也让
没有 Policy 的 Runtime 无法复用纯路由流程。

此外，commit `4d0216b` 已让 parser 对 malformed input 返回 `root=None`，但 Kernel
仍会把该结果交给 Policy，Custom Registry 也仍可按 token 匹配 malformed command。
RFC-0008 要求在拆分路由与授权的同时建立明确的 Parsed Command validation boundary。

RFC-0008 要求 Router 只回答“由哪个 Command 执行、是否可并行”，Policy 只回答
“当前命令是否允许”。两者都必须在 Scheduler admission 前完成，但彼此不依赖。

## 影响

完成后，Parsed Command 和 Route 会成为主链路中唯一需要继续传播的对象。Router 可被
独立测试，Supervisor 不再理解 Policy metadata，Scheduler 继续只消费可信的
`parallel_safe`。

## 变更

- 将 Router 核心接口改为
  `resolve(command: ShellParseResult) -> _ExecutionRoute`。
- 在 Router 和 Policy 前验证 Parsed Command；root 缺失、空输入、error node 或
  missing node 立即返回 `invalid_argument` 和 `invalid shell command`。
- 保证 parse failure 不进入 Custom Registry、Policy、Supervisor、Capability View、
  Scheduler 或执行，因此 malformed custom command 不再由 Custom handler 处理。
- 让 Router 按现有 custom-first 规则选择 Custom command 或 Shell fallback。
- 由 Route 保存选中的 `_Command` 与 Runtime-trusted `parallel_safe`。
- 保留显式 `parallel_commands` 的现有语义；Shell Command 从 AST executable 与
  composition 派生调度结论，Custom Command 使用自身元数据。
- 删除 `ExecutionDecision`，并移除 Router、Supervisor、Scheduler 和 Execution 对
  `PolicyEvaluation`、`PolicyAction` 的依赖。
- 将 Supervisor admission 改为接收原始 `ShellParseResult` 与 Route。
- 证明 Router 不启动子进程、不执行用户交互、不占用 Scheduler 容量。
- 将 `tools run PY<< ... PY` 这种非法 Shell 语法的模型可见输入改写为合法 heredoc
  `tools run <<'PY' ... PY`，避免被新 boundary 误杀；旧 `PY<<` 形式不再绕过 parser。

## 验收标准

- [x] Router 的输入和输出中不存在 Policy 或 approval 类型。
- [x] 所有 parse failure 都返回稳定的模型可见错误，且不会产生 Handle、Scheduler
      item、Capability preparation 或子进程。
- [x] malformed custom command 不再绕过 parser validation 进入 Custom handler。
- [x] Custom command 与 Shell fallback 仍遵守相同的调度、执行、取消和结果链路。
- [x] 显式 `parallel_commands` 与 Custom 的 `parallel_safe` 行为不回退。
- [x] Supervisor 和 Scheduler 无法读取 Policy metadata。
- [x] 路由失败或后续授权失败都不会创建 Execution。
