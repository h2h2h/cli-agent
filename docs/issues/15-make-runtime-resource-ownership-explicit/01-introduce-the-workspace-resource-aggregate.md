# refactor(runtime): introduce the Workspace resource aggregate

**Status / 状态：** pass

## 背景

Runtime open 当前在 `AgentRuntime._reconcile()` 中依次准备 Workspace、加载
environment 快照、打开 Capability View，并调和 MCP 投影、Tool Catalog、Tool
Environment 与 Skill Catalog。调和结果随后作为多个独立参数传给
`AgentRuntime`，Workspace-lifetime 的所有权只能从字段用途推断。

RFC-0006 选择以一个私有、引用稳定的纯数据聚合表达当前 Runtime-owned
Workspace resource，并以模块级函数保留现有调和顺序。该边界不提供动态查找、
注册机制或无实际行为的关闭协议。

## 影响

完成后，项目将获得一个可命名、可类型检查、可独立测试的 Workspace resource
所有权边界。现有 Runtime 行为、持久格式、公开 API 与模型可见 Syscall 不发生
变化，并为后续 `AgentRuntime` 迁移提供单一输入。

## 变更

- 新增 `src/cli_agent/runtime/_resources.py`。
- 定义 frozen、slotted 的 `_RuntimeResources` dataclass，仅包含当前具有 Runtime
  消费者的字段：
  - Workspace root；
  - immutable base environment snapshot；
  - `_CapabilityView`；
  - `_ToolCatalog`；
  - `_ToolEnvironment`；
  - `_SkillCatalog`。
- 将 `base_env` 标记为 `repr=False`，避免调试表示包含 Workspace environment
  value。
- 实现模块级异步函数 `_reconcile_runtime_resources()`，保持当前顺序：
  Workspace preparation → environment loading → Capability View → MCP projection
  → Tool Catalog → Tool Environment → Skill Catalog。
- 继续执行 `_MCPCatalog.reconcile()`，但不把没有 Runtime 消费者的返回对象保存
  到 `_RuntimeResources`；生成的 Tool 文件仍由后续 Tool Catalog 收集。
- 保持各 reconciler 现有的异常、fail-soft、原子写与持久状态语义，不复制其业务
  逻辑。
- 新增 `tests/test_runtime_resources.py`，验证字段组成、base environment
  不可变性与 repr、调和顺序、Tool Environment fail-soft 状态，以及失败传播与
  当前行为一致。
- 保持 `_resources.py` 不导入 `_environment`，并且 `_capability` 不反向导入
  `_resources`。
