# test(runtime): verify AST routing and policy boundaries

**状态：** resolved

## 背景

RFC-0008 同时调整 parser failure、Router 输入、可选 Policy、Host interaction、
Scheduler admission、Capability View 和 system message。只依赖各模块单元测试无法证明
命令没有在层间重新耦合，也无法证明被拒绝或解析失败的请求没有创建执行资源。

本 issue 负责跨层验证和文档收尾，不增加新的运行期功能。

## 影响

完成后，项目会用 contract tests 固化唯一主链路，证明现有 Approver 和
ExecutionDecision 已从公共 API 与运行期依赖中移除，同时证明被否决的 Catalog、
`ShellEffect` 和 Composite Facts 没有被引入。RFC、架构文档、discussion 和 README
将描述同一套边界。

## 变更

- 增加从 `exec` 到 parse、route、optional policy、admission 和 execution 的跨层测试。
- 覆盖 parse failure、unsupported valid syntax、Custom route、Shell fallback、
  `policy=None`、ALLOW、DENY、ASK、Policy 异常和非法 evaluation。
- 覆盖 `allow_once`、deny、cancel、interaction 异常、非法 option，以及 Session/
  Runtime close 取消 pending ask。
- 断言失败和拒绝路径不会产生 Handle、Scheduler item、Capability preparation 或子进程。
- 覆盖批次顺序、显式 `parallel_commands`、Custom `parallel_safe` 和独立读取并行行为。
- 覆盖 Capability View AST redirects 与私有 mutation rules。
- 检查现有 `ExecutablePolicy`、`ExecutionApprover`、`ExecutionApprovalRequest`、
  `ApprovalResponse`、`_ExecutionApprovalGate` 和 `ExecutionDecision` 已被删除。
- 检查实施过程没有引入 `shell_catalog`、`ShellEffect` 或 Composite Facts。
- 更新 `docs/architecture.md`、相关 discussion、README 和示例 Host 调用，记录
  `user_interaction` 必选、`execution_policy` 可选以及单 Runtime/单 Session 假设。
- 运行完整测试、类型检查、lint 和文档链接检查，提交 peer review。

## 验收标准

- [x] 跨层测试证明 parse failure 和所有 fail-closed 分支均无执行副作用。
- [x] `policy=None` 与 configured Policy 两条路径都被端到端覆盖。
- [x] Runtime close、Session close 与普通失败后的 Session 可用性均有测试。
- [x] 旧 approval 和 decision 类型不再存在于代码、公共 API 或活动文档中。
- [x] Catalog、`ShellEffect` 和 Composite Facts 未被引入运行期或公共 API。
- [x] RFC、架构文档、discussion、README 与实现描述一致。
- [x] 所有自动化检查通过，并完成 peer review 后才将各 issue 标记为 `resolved`。
