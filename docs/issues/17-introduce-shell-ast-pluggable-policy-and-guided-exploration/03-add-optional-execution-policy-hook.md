# feat(runtime): add optional execution policy hook

**状态：** resolved

## 背景

Runtime 当前默认构造具体 `ExecutablePolicy`，并通过携带 parse result 的 evaluation 和
final decision 控制主链路。这样即使 Host 不需要授权策略，命令仍必须经过 Policy
对象；具体默认策略也与 Runtime 本身绑定。

RFC-0008 将 Policy 定义为可选、Host 注入的插件。本 issue 只建立插件边界和失败语义，
不设计命令黑白名单或其他具体策略。

## 影响

完成后，Host 可以在 Runtime open 时选择是否启用一个 Policy。未配置时主链路直接从
Route 进入 Supervisor；配置时 Custom 和 Shell fallback 都在 admission 前经过同一
Policy。Policy 的实现可以位于项目外部，而无需依赖 Runtime 的 Catalog 或 UI。

## 变更

- 定义只有异步 `evaluate(ShellParseResult)` 方法的 `ExecutionPolicy` Protocol。
- 将 `PolicyEvaluation` 收敛为 `action`、`rule_id` 和可选 `reason`，不包含 command。
- 保留 `PolicyAction.ALLOW`、`ASK`、`DENY`，不增加多 Policy chain 的
  `CONTINUE`。
- 为 `AgentRuntime.open` 增加可选 `execution_policy`；`None` 表示完全跳过，不构造
  默认 Policy 或隐式 decision。
- 对每个成功解析、成功路由的 Custom 和 Shell fallback 各 evaluate 一次。
- 禁止 Policy 接收 route、cwd、Session context 或 `UserInteraction`，也禁止改写 AST。
- 固定 Policy 的 Runtime 生命周期，不支持动态发现、热加载、替换或多 Policy 组合。
- Policy 返回 `DENY` 时以 `policy_denied` 阻止当前命令。
- Policy 抛出异常或返回非法结果时 fail closed，以通用 `policy_denied` 返回模型，将
  详情写入 Host diagnostic，并保持 Session 可用。
- Kernel 接收 `on_diagnostic` 回调，把 Policy 异常与非法返回写入 Host diagnostic。
- Reference CLI 的 `run_agent` 增加可选 `execution_policy`，转发给
  `AgentRuntime.open`。
- 删除 `ExecutablePolicy`，本 milestone 不增加任何内置具体 Policy。

## 验收标准

- [x] `policy=None` 路径不调用或构造任何 Policy/Decision 对象。
- [x] 外部对象只实现 Protocol 即可被注入。
- [x] Custom 和 Shell fallback 使用完全相同的 Policy hook。
- [x] Policy 不能改变 Parsed Command 或 Route。
- [x] DENY、异常与非法返回均阻止当前命令，但不终止 Session。
- [x] 代码库中不存在具体内置 Policy 策略。
