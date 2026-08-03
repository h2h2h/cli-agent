# refactor(capability): localize AST-based shell preparation

**状态：** resolved

## 背景

commit `4d0216b` 已将 `prepare_shell` 改为接收 `ShellParseResult`，输出重定向也已经
通过 AST redirect 节点取得。当前剩余问题不是迁移到 AST，而是职责归属：
`_DIRECT_MUTATORS` 和 `_sed_is_in_place` 仍定义在 command parser 中，再由 Capability
View 导入；测试 Policy 也复用了 `_DIRECT_MUTATORS`。

这些规则解释的是哪些路径需要 copy-up、whiteout 与 materialization，属于 Capability
View 语义，不是通用 Shell 语法事实。RFC-0008 要求将它们收回 Capability View，同时
不引入 Catalog、`ShellEffect` 或 Composite Facts。

## 影响

完成后，command parser 只保留 AST 与派生语法事实；`cp`、`mv`、`rm`、`sed` 等
mutation target 规则只存在于 Capability View。现有基于 AST 的重定向、copy-up、
whiteout 和 materialization 行为保持不变，这些启发式规则也不会被误当成 Policy 或
Scheduler 可依赖的通用安全分类。

## 变更

- 保留现有 `prepare_shell(ShellParseResult)` 和 AST redirect 遍历行为。
- 将 `_DIRECT_MUTATORS`、`_sed_is_in_place` 以及 mutation target 推断所需的命令规则
  移入 Capability View 私有实现。
- 让 command parser 不再导出或维护 Capability mutation 语义。
- 让 Policy 测试 fake 自己声明所需策略，不能复用 Capability View 的 mutation list。
- 不引入 `shell_catalog`、`ShellEffect`、Composite Facts 或其他共享命令语义层。
- 禁止 Capability View 把派生事实写回 AST 或提供给 Policy、Router、Scheduler。
- 保持 Custom command 的状态准备由各自 handler 负责。
- 补充 mutation rule 所有权测试，并保留现有重定向、变更目标与 unsupported syntax
  覆盖。

## 验收标准

- [x] `prepare_shell` 继续直接消费 `ShellParseResult`，redirect 继续来自 AST。
- [x] 现有重定向、copy-up 和 whiteout 行为由 AST 驱动且不回退。
- [x] command parser 不包含 executable mutation list 或 Capability 专属判断。
- [x] mutation target 规则只存在于 Capability View 边界，Policy 不复用这些规则。
- [x] 代码没有引入 Catalog、`ShellEffect` 或 Composite Facts。
- [x] parse failure 无法进入 Capability View。
- [x] Custom command 状态准备不被 Shell AST 迁移破坏。
