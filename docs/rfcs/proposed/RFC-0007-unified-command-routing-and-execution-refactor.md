---
rfc_id: RFC-0007
title: Unified Command Routing and Execution Refactor
status: PROPOSED
author: cli-agent maintainers
reviewers:
  - name: project owner
    status: pending
created: 2026-08-02
last_updated: 2026-08-02
related_prds: []
related_rfcs:
  - RFC-0001-host-mediated-execution-approval.md
  - RFC-0002-workspace-capability-view.md
  - RFC-0003-tool-capability-commands.md
  - RFC-0006-explicit-runtime-resource-ownership.md
---

# RFC-0007: 统一 Command 路由与执行模型重构

## 概述

本 RFC 计划重构 Environment 的命令路由与执行边界：将普通 Shell 命令和
Runtime-owned Custom 命令统一抽象为 `_Command`，移除 `_DriverKind` 与
`_ExecutionLane`，让 Scheduler 只根据一个 Runtime-trusted 的
`parallel_safe` 结果进行有界并行调度。

`tools list`、`tools info` 与 `tools run` 不再拥有独立的 Tool Driver 类型，而
是作为名为 `tools` 的 Custom command 注册到 `_CustomCommandRegistry`。`cd`、
`export` 与 `tools` 的 prepare 和具体执行代码迁移到新的 command handler 目录。

Tool 文件可以在模块顶层声明：

```python
PARALLEL_SAFE = True
```

Runtime open 时，Tool Catalog 从有效的 Capability View 文件中静态读取这一声明，
并将其记录到 `ToolEntry`。普通 Tool 缺失声明时默认为 parallel-safe，MCP Tool
缺失声明时默认为串行。`tools run` 只有在所有静态引用的 Tool 都是
parallel-safe，且没有动态引用时，才会被路由为可并行命令。声明或源代码解析失败
时使用对应默认值，并通过 `RuntimeDiagnostic` 提示，不阻断 Runtime open。

本 RFC 同时将 `CommandParseResult` 收敛为纯语法事实，移除其中的 `tool` 字段。
Tool grammar 仍可产生 `ToolCommand`，但该事实只在 tools custom handler 和路由
调度判断中使用，不再污染通用命令解析结果、Policy 或 Approval Request。

本 RFC 不新增模型可见 syscall，不改变 `exec`、`output`、`kill` 的协议，不引入
操作系统级 sandbox，也不承诺验证 Tool 作者声明的 parallel-safe 真实性。

## 背景与上下文

### 当前实现

当前执行链路为：

```text
exec
  -> parse_shell_command
  -> classify_tool_command
  -> Policy
  -> Router
  -> Scheduler
  -> Supervisor
  -> Shell / Custom / Tool Driver
```

当前实现存在四类耦合：

1. `CommandParseResult.tool` 让通用 Shell parser 依赖 tools capability grammar。
2. Kernel、Policy、Approval Request、Router 都读取 `command.tool`。
3. `_DriverKind.TOOL` 同时承担路由分类、独立 lane 和 Session context 隔离三个
   不同职责。
4. Tool 是否并行由 Runtime/Host 的 `parallel_tools` 名单决定，Tool 文件自身无法
   声明其并行安全属性；普通 Tool 和 MCP Tool 也没有不同的默认策略。

当前 RFC-0003 还将 Tool 工作放入独立 Tool lane，以避免长时间运行的 Shell 阻塞
Tool。新的设计接受全 Session 统一调度的行为：任何命令都必须遵守同一个有序的
parallel-safe barrier，不再因为 command 类型不同而跨越串行命令。

### 当前耦合位置

- `CommandParseResult.tool` 位于
  `src/cli_agent/runtime/_capability/command_parser.py`。
- tools 解析在 Kernel 中提前完成，位于
  `src/cli_agent/runtime/_environment/kernel.py`。
- Tool special route、Tool lane 和 `_DriverKind.TOOL` 位于
  `src/cli_agent/runtime/_environment/routing.py`。
- Tool lane 容量和 lane claim 位于
  `src/cli_agent/runtime/_environment/scheduler.py`。
- Tool-specific context isolation 位于
  `src/cli_agent/runtime/_environment/supervisor.py`。
- `cd`、`export` 的 prepare 当前与 Registry 放在同一个
  `commands.py` 中。

### 术语

| 术语 | 定义 |
|---|---|
| Command | Runtime 识别并负责准备一次执行的命令族对象。 |
| Shell Command | 未命中 Custom registry 后的 Shell fallback command。 |
| Custom Command | 由 Runtime 代码注册、按 command name 精确匹配的命令。 |
| Tool Command | 顶层 `tools` custom command 的 grammar 事实，不是通用 Command 类型。 |
| Prepared Execution | Command.prepare 产生的一次具体、可运行、可取消的 Execution 实例。 |
| isolated | Command 是否使用 Session context 的隔离快照。 |
| parallel-safe | Runtime 是否允许该具体命令与其他 parallel-safe 命令并行。 |
| Tool Catalog | Runtime open 时从有效 Capability View Tool 文件派生的不可变快照。 |

## 问题陈述

### 需要解决的问题

当前 Tool 是路由、调度和执行中的特殊分支，而不是普通 Runtime-owned command 的
一个实例。这导致新增 Custom command 时必须同时理解 Tool 特殊路径、Tool lane 和
parser enrichment。

同时，Scheduler 的并发决策不应知道 Shell、Tool 或 Custom 的类别。它只应接受
上游已经完成的 Runtime-trusted scheduling decision。命令类型和调度属性是两个
正交维度：一个串行 Tool 仍然可以是 Custom，一个隔离的 Shell command 也可以是
parallel-safe。

### 不重构的影响

- Parser 继续依赖 capability-specific facts。
- 新的 Custom command 需要添加新的 Router 分支。
- Tool 并行能力继续由全局字符串名单描述，无法随 Tool 文件一起发现和审查。
- Supervisor 必须继续通过 driver kind 推断 context 语义。
- 删除 Tool lane 时会留下未定义的调度和隔离行为。

## 目标与非目标

### 目标

1. 将 `_DriverKind` 完全移除，使用 `_Command` 对象表达已解析的命令族。
2. 将 `_ExecutionLane` 完全移除，Scheduler 只处理全局有界的
   `parallel_safe` 调度。
3. 让 Parser 只产生通用 Shell 语法事实。
4. 让 `cd`、`export`、`tools` 统一由 Custom registry 管理。
5. 让 `tools` 的非法语法始终进入 Custom handler，不能落到 Shell fallback。
6. 让 Tool 文件可以声明模块级 `PARALLEL_SAFE`，缺省为串行。
7. 将 `isolated` 作为 Command 元数据，而不是从 Driver kind 推断。
8. 保留统一的 Policy、Execution State、output、kill、close 和结果顺序语义。
9. 保留跨 Session 并发，取消同一 Session 内的 Tool-specific lane。
10. 在不新增模型 syscall 的前提下完成所有行为迁移。

### 非目标

1. 验证 `PARALLEL_SAFE = True` 是否真实安全。
2. 为不同 Tool 函数声明不同的并行属性。
3. 引入 OS sandbox、网络隔离或资源隔离。
4. 动态重新加载已打开 Runtime 的 Tool Catalog。
5. 让 Workspace 文件直接注册新的顶层 Runtime custom command。
6. 为每个 Command 引入独立的模型可见 schema。
7. 保留 `parallel_tools`、`tool_parallel_limit` 或 `_DriverKind.TOOL` 的兼容层。
8. 保留 `ToolCommand` 作为 Runtime 的公开 API 类型。

## 设计原则

### 语法、授权、路由和执行职责分离

```text
CommandParseResult（语法事实）
        ↓
Policy / Approval（是否允许）
        ↓
Command Router（选择 Command）
        ↓
Command.parallel_safe（是否可并行）
        ↓
Scheduler（有序 admission）
        ↓
Command.prepare（创建 Prepared Execution）
        ↓
Execution Supervisor（运行、观察、取消、清理）
```

Parser 不识别 Tool invocation。Policy 不需要知道 command 是否为 Tool。Router
不执行 command。Scheduler 不知道 command 类型。Command handler 负责解析自己
的专属 grammar 并准备执行对象。

### 并行安全不是安全边界

`PARALLEL_SAFE` 是调度声明，不是权限声明，也不是 sandbox。Tool 已经在现有
Tool Environment 和 Worker 语义下执行；错误声明可能造成并发竞态，但不会绕过
Policy、Execution ownership 或 Workspace Capability View 的已有边界。

缺省串行，只有显式声明才获得并行资格，避免新增 Tool 在未审查时改变调度语义。

### isolated 与 parallel-safe 正交但有约束

一个命令可以：

- `isolated=False`、`parallel_safe=False`：例如 `cd`、`export`；
- `isolated=True`、`parallel_safe=False`：例如有副作用但必须串行的 `tools run`；
- `isolated=True`、`parallel_safe=True`：例如静态引用全部 parallel-safe Tool 的
  `tools run`。

不允许 `parallel_safe=True` 的命令使用可变 Session context。Supervisor 应将
`parallel_safe` 作为强制隔离条件，并在 Command 元数据不一致时 fail closed。

## 方案比较

### 方案一：保留 Tool 特殊 Driver，缩减 `_DriverKind`

只将 `_DriverKind` 的枚举值改为 `SHELL` 和 `CUSTOM`，但继续保留
`command.tool`、`_ToolDriver`、Tool lane 和 `parallel_tools`。

**优点**

- 代码改动较小。
- 现有 Tool lane 行为保留。

**缺点**

- Tool 仍然不是统一 Custom command。
- Parser、Policy 和 Router 继续依赖 Tool facts。
- `_DriverKind.CUSTOM` 会同时代表 `cd`、第三方 custom 和 tools，语义仍然不清晰。
- 无法自然表达 Tool 文件自身的 parallel-safe 声明。

**结论**：不采用。该方案只修改枚举名称，无法解决本 RFC 的结构性耦合。

### 方案二：只保留 `_CustomCommandSpec`，不抽象 Command 基类

让 Shell 继续是一个独立 fallback Driver，Custom command 继续由
`_CustomCommandSpec` 表示；路由和 Supervisor 继续通过特殊字段判断隔离。

**优点**

- 迁移成本低。
- 可以快速将 tools 注册到 Custom registry。

**缺点**

- Shell 和 Custom 的匹配、prepare、parallel-safe、isolated 元数据没有统一
  合同。
- Route 仍需要额外的 `driver`、`driver_kind` 或特殊判断。
- 后续新增命令族时会重新扩展 Router 分支。

**结论**：不采用。它可以作为临时迁移步骤，但不作为最终模型。

### 方案三：统一 `_Command` 基类，使用单一 Scheduler

引入 `_Command` 抽象基类，由 `_ShellCommand` 和 `_CustomCommand` 派生。每个
Command 提供匹配、prepare、isolated 和 parallel-safe 判断。Router 只做
Custom-first lookup，Scheduler 只处理 boolean parallel-safe。

**优点**

- Command 类型、执行准备、隔离元数据和调度判断形成一个稳定合同。
- Tool 可作为普通 Custom command 处理。
- Scheduler 不依赖 Driver 或 command 类型。
- 删除 `_DriverKind` 和 `_ExecutionLane` 后仍然保留明确的执行责任边界。
- Tool 文件声明可以直接进入 `ToolEntry` 和 Custom command 的 scheduling 判断。

**缺点**

- 同一 Session 不再有 Tool lane，Tool 可能等待串行 Shell command。
- 需要更新 RFC-0003 的 Tool scheduling 语义和 Runtime API。
- Tool metadata 是作者声明，不能由 Runtime 静态证明。

**结论**：采用。

## 技术设计

### Command 抽象

建议在 `src/cli_agent/runtime/_environment/commands.py` 中保留 Registry 和
Command 抽象；具体 handler 放入新的 `handlers/` 目录。

Command 基类的目标合同如下：

```python
class _Command(ABC):
    name: str | None
    isolated: bool

    @abstractmethod
    def matches(self, command: CommandParseResult) -> bool:
        ...

    @abstractmethod
    def parallel_safe(self, command: CommandParseResult) -> bool:
        ...

    @abstractmethod
    def prepare(
        self,
        command: CommandParseResult,
        context: _CommandContext,
    ) -> _PreparedExecution:
        ...
```

其中：

- `name` 对 Custom command 是精确匹配名；Shell command 使用 `None` 表示 fallback。
- `isolated` 是静态 Command 属性。
- `parallel_safe()` 可以是固定判断，也可以根据具体 parse result 和 Catalog facts
  动态判断。
- `prepare()` 不启动进程，不修改 Session 状态，只构造 Prepared Execution。

`_ExecutionRoute` 简化为：

```python
@dataclass(frozen=True, slots=True)
class _ExecutionRoute:
    command: _Command
    parallel_safe: bool
```

不再包含：

- `driver_kind`；
- `lane`；
- `driver`；
- Tool-specific scheduling facts。

Command 自身拥有 prepare 行为，因此 route 不再需要额外持有 Driver。一次被
admit 的 route 持有不可变的 Command 引用，后续 Registry 变化不会改写已经提交的
执行。

### Shell Command

`_ShellCommand` 是 Router 的固定 fallback command：

- 只有当 Custom registry 未匹配时才使用；
- `isolated=True`；
- 继续通过 `create_subprocess_shell` 执行完整 raw command；
- `parallel_safe=True` 只在 tokenization 成功、executable basename 位于
  `parallel_commands`、且不包含 Shell composition 时成立；
- Shell composition、重定向、命令替换和控制操作默认串行，Policy 仍然独立处理
  其授权。

`parallel_commands` 保留，因为它描述的是 Host/Runtime 对普通 Shell basename
的调度信任，不属于 Tool-specific 配置。

### Custom Command 与 Registry

`_CustomCommandRegistry` 统一管理所有 Runtime-owned custom command：

- `cd`；
- `export`；
- `tools`；
- 后续 Runtime 内建命令。

Registry 只由 Runtime 代码注册。Workspace Tool 文件不能注册新的顶层 command，
只能通过 `tools run` 被调用。

默认保留命令名冲突保护：`cd`、`export`、`tools` 是 Runtime 保留名，重复注册
应失败；第三方 Runtime custom command 也不应静默覆盖既有命令。若未来需要覆盖，
应增加显式替换 API，而不是复用普通 `register()` 的隐式覆盖语义。

Registry 的匹配规则：

1. 先按第一个命令 token 精确查找 Custom name；
2. `tools` 的 malformed command 也必须依据原始 command head 命中 Custom；
3. `./tools`、`/bin/tools`、`toolsmith` 不命中名为 `tools` 的 Custom；
4. 命中后即使存在 pipeline、redirection 或非法参数，也不得 fallback 到 Shell；
5. Custom handler 负责对命令整体语义返回失败结果。

为支持 tokenization 失败时的保留命令匹配，需要提供一个不依赖完整
`shlex.split()` 成功与否的 command-head 判断。该判断仍属于语法事实，不执行
命令、不执行 Tool，也不参与 Tool classification。

### Built-in Custom Command 属性

| Command | isolated | parallel-safe | 说明 |
|---|---:|---:|---|
| `cd` | false | false | 修改 Session cwd。 |
| `export` | false | false | 修改 Session custom environment。 |
| `tools` list | true | true | 读取 Catalog projection。 |
| `tools` info | true | true | 读取 Catalog entry。 |
| `tools` run | true | 动态 | 由引用的 ToolEntry 和 grammar facts 决定。 |

`cd` 和 `export` 的 `_prepare_cd`、`_prepare_export` 从 `commands.py` 移到
对应 handler 文件。Registry 只保存 Command 描述，不保存具体业务执行代码。

### Tools Grammar 与 Policy 解耦

`CommandParseResult` 只保留：

- raw command；
- tokens；
- executable basename；
- tokenization result；
- Shell composition fact；
- output redirection fact。

`tools/grammar.py` 的接口改为返回 tools-specific facts：

```python
def parse_tool_command(
    command: CommandParseResult,
    catalog: _ToolCatalog,
) -> ToolCommand | None:
    ...
```

它不再通过 `dataclasses.replace()` 给 CommandParseResult 注入 `tool`。

`tools` Custom command 的 `parallel_safe()` 与 `prepare()` 都可以调用该纯函数：

- `parallel_safe()` 只计算路由所需的调度结果；
- `prepare()` 再根据同一 raw command 生成实际执行；
- classification 不产生外部副作用，也不启动 Tool worker；
- 非 tools command 返回 `None`，但正常流程中只有 Registry 命中 `tools` 时才会调用。

`ToolCommand` 保留在 capability tools 内部，作为 grammar facts 使用，但从
`cli_agent.runtime` 公共导出中移除。

### Policy 与 Approval Request

`ExecutablePolicy` 不再导入或读取 `ToolCommand`，也不再产生
`tool.<operation>.allow` rule。它只根据通用 CommandParseResult 判断：

- executable basename allow/deny/ask；
- output redirection；
- in-place `sed`；
- default action。

默认 Policy 的 default action 是 allow，因此普通 `tools` 命令在默认设置下仍可
运行。Host 明确将 `tools` 加入 deny 或 ask 时，按普通 executable policy 处理。

`ExecutionApprovalRequest` 删除 `tool` 字段，只保留通用 parse facts、rule id
和 reason。Approval 发生在 Router 之前，因此 custom handler 仍不能绕过 Policy。

Kernel 中删除：

- `classify_tool_command` import；
- parse 后的 Tool enrichment；
- `command.tool` equality 和 existence 逻辑。

Kernel 仍负责把 Tool Catalog 和 Tool Environment 注入 `tools` Custom command。
如果 Tool Environment 不可用，`tools list` 和 `tools info` 仍可执行，`tools run`
由 handler 返回结构化失败结果，不 fallback 到 Host Python。

### Tool Catalog 的 parallel-safe 元数据

`ToolEntry` 增加：

```python
parallel_safe: bool
```

Catalog 在现有 AST parse 阶段静态读取模块级声明：

```python
PARALLEL_SAFE = True
```

解析规则：

1. 只接受模块顶层 `Assign` 或 `AnnAssign`；
2. 只接受字面量 `True` 或 `False`；
3. 普通 Tool 缺失声明等同于 `True`，MCP Tool 缺失声明等同于 `False`；
4. 多次声明、非布尔值或源代码解析失败时，使用对应默认值并发送
   `tools.parallel_safe_parse_failed` RuntimeDiagnostic；
5. 不 import、不执行 Tool 模块；
6. Workspace effective file 的声明覆盖 repertoire lower file 的声明。

建议在生成的 `tools/index.md` 和 `tools info` 中展示 parallel-safe 状态，便于
用户审查；该 projection 仍然不是 Runtime authority。

`tools run` 的 parallel-safe 规则：

```text
operation == list                         -> true
operation == inspect                      -> true
operation == run
  and valid
  and references 非空
  and no dynamic references
  and every referenced ToolEntry.parallel_safe -> true
```

其他情况为 false。该规则将当前 `parallel_tools` Host allowlist 替换为 Tool
Catalog 的显式声明。若未来需要 Host 上限，可在不恢复 Tool lane 的前提下增加
一个全局并行预算或 Policy constraint，但不在本次重构中保留旧的按名称配置。

### Scheduler

Scheduler 删除 lane 概念和所有 Tool-specific capacity：

- 删除 `_ExecutionLane`；
- 删除 `_claim_lane()`；
- 删除 `_lane_limit()`；
- 删除 `tool_parallel_limit`；
- 只保留一个 `parallel_limit`；
- Scheduler 只读取 `route.parallel_safe`。

采用一个按提交顺序维护的 pending queue，并使用 serial barrier：

1. Queue head 是 parallel-safe 时，连续的 parallel-safe commands 可以批量 claim；
2. 批次最多占用 `parallel_limit` 个运行槽位；
3. Queue head 是 serial command 时，必须等待所有更早运行项结束；
4. serial command 独占运行槽位；
5. serial barrier 后的 parallel-safe command 不能越过该 barrier；
6. pending queue 继续使用现有 `queue_limit`；
7. denied command 不创建 Execution，也不占用 queue capacity。

例如：

```text
parallel A, parallel B, serial C, parallel D, parallel E
       └──── batch 1 ────┘  └── barrier ──┘  └── batch 2 ──┘
```

不同 Session 之间仍然可以并行；同一 Session 不再区分 Shell/Tool lane。

### Supervisor 与 isolated context

Supervisor 不再读取 `_DriverKind`。它依据 route command metadata 创建
`_CommandContext`：

- `command.isolated` 为 true 时复制 `environment`，并不提供 `set_cwd`；
- `route.parallel_safe` 为 true 时强制使用同样的隔离快照；
- 其他 serial Session-mutating command 可以获得可变 Session environment 和
  `set_cwd`。

这样 `cd`、`export` 可以顺序修改 Session 状态，parallel-safe command 不会把
并发执行中的修改写回 Session。Tools worker 即使 serial，也使用 isolated context。

### 目录和模块重命名

当前 `drivers/` 同时包含 Command prepare、Tool special driver、Shell driver、
Execution protocol 和 process adapters，名称已经不能准确表达职责。

建议重命名为 `handlers/`，目标结构为：

```text
src/cli_agent/runtime/_environment/
├── commands.py                 # _Command、_ShellCommand、_CustomCommand、Registry
├── handlers/
│   ├── __init__.py
│   ├── base.py                 # _CommandContext、Prepared Execution contracts
│   ├── cd.py                   # cd prepare/execution
│   ├── export.py               # export prepare/execution
│   ├── shell.py                # Shell command prepare/execution
│   ├── tools.py                # tools custom command prepare/execution
│   └── executions.py            # Inline / subprocess concrete executions
├── execution_state.py          # Session Execution State
├── routing.py
├── scheduler.py
└── supervisor.py
```

迁移关系：

| 当前 | 目标 | 说明 |
|---|---|---|
| `drivers/base.py` | `handlers/base.py` | 去除 Driver 命名，保留通用 execution contracts。 |
| `drivers/executions.py` | `handlers/executions.py` | 保留 Inline/Process execution。 |
| `drivers/shell.py` | `handlers/shell.py` | 改为 `_ShellCommand` 的 prepare 实现。 |
| `drivers/tool.py` | `handlers/tools.py` | 改为 `tools` Custom command handler。 |
| `commands.py::_prepare_cd` | `handlers/cd.py` | 迁移 Session cwd mutation。 |
| `commands.py::_prepare_export` | `handlers/export.py` | 迁移 Session env mutation。 |
| `_DriverContext` | `_CommandContext` | 消除 Driver 语义。 |
| `_DriverExecution` | `_PreparedExecution` | 表达一次已准备的可运行执行。 |
| `_ExecutionDriver` | `_Command.prepare` | prepare 归属统一 Command。 |

`execution_state.py` 不迁移到 `handlers/executions.py`，避免与已有 Session
Execution State 模型混淆。

## 执行流程

### 默认 Shell 命令

```text
exec("cat file")
  -> parse_shell_command
  -> ExecutablePolicy
  -> registry miss
  -> ShellCommand
  -> parallel_safe(command)
  -> Scheduler
  -> ShellCommand.prepare
  -> ProcessExecution
```

### `cd` / `export`

```text
exec("export KEY=value")
  -> parse_shell_command
  -> ExecutablePolicy
  -> registry hit: export
  -> serial, non-isolated route
  -> ExportCommand.prepare
  -> InlineExecution
  -> Session environment mutation
```

### Tools

```text
exec('tools run "tools.math.add(1, 2)"')
  -> parse_shell_command
  -> ExecutablePolicy
  -> registry hit: tools
  -> parse_tool_command for route scheduling
  -> ToolEntry.parallel_safe facts
  -> Scheduler
  -> ToolsCommand.prepare
  -> fresh Tool worker ProcessExecution
```

非法或不完整的 `tools` 命令仍然走 `tools` Custom handler，输出 validation
failure，不启动 Shell。`ToolCommand` grammar facts 不进入 Policy 或 Approval
Request。

## 兼容性与行为变化

本 RFC 遵守项目“不保留 backward compatibility”的代码重构原则，以下接口和
语义会被删除或改变：

1. 删除 `CommandParseResult.tool`。
2. 删除 `ExecutionApprovalRequest.tool`。
3. 删除 `ToolCommand` 的 Runtime 公共导出。
4. 删除 `_DriverKind`。
5. 删除 `_ExecutionLane` 及 `route.lane`。
6. 删除 `parallel_tools` 和 `tool_parallel_limit`。
7. 删除 `_ToolDriver` special route。
8. Tool 不再拥有独立 lane，可能被同一 Session 内更早的 serial Shell command
   阻塞。
9. `tools` 不再通过 `tool.<operation>.allow` rule 绕过 default Policy action。
10. Tool 并行资格改由有效 Tool 文件的 `PARALLEL_SAFE` 声明决定。

RFC-0003 的 Tool Catalog、Tool Environment、Worker、fresh process、output、kill
和 Capability View 相关设计继续有效；其 Tool lane、`parallel_tools`、Tool facts
注入 Parser/Policy 的部分由本 RFC supersede。

## 安全与信任边界

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| Tool 错误声明 parallel-safe | 并发竞态或数据覆盖 | 普通 Tool 缺失声明默认为 true、MCP Tool 默认为 false；只接受显式静态 bool，解析失败回退并发送诊断；文档明确声明不是安全保证。 |
| `tools` 非法语法落到 Shell | 绕过 Runtime Tool handler | 原始 command head 匹配；Custom 命中后禁止 fallback。 |
| Workspace Tool 影响并发策略 | 调度行为变化 | Runtime open 固定 Catalog 快照；有效 Workspace 文件优先。 |
| Custom command 修改 Session 状态 | Session race | 修改命令固定 serial、non-isolated；parallel-safe 强制 isolated。 |
| Tool handler 绕过 Policy | 未授权执行 | Router 只接受 final ExecutionDecision，Policy 仍在 Router 之前。 |
| Catalog projection 被篡改 | 错误的 Tool 或并行判断 | `index.md` 仍不是 authority，所有 facts 从有效源文件派生。 |
| Worker Python fallback | 执行到 Host 环境 | 继续要求 private Tool Environment；不可用时 run fail，不 fallback。 |

本 RFC 不将 `PARALLEL_SAFE` 当作能力授权，也不提供 Tool 代码级别的资源或网络
沙箱。Tool 仍然继承现有 RFC-0003 的 Runtime/Worker 信任边界。

## 实施计划

### 阶段 0：基线与契约测试

- 固化当前完整测试基线。
- 增加重构后的 route、scheduler、approval 和 Tool Catalog 行为清单。
- 标记 RFC-0003 中将被 supersede 的 Tool lane 测试和 API 测试。
- 不修改用户工作区内容，不生成兼容 shim。

### 阶段 1：引入 Command 合同

- 新增 `_Command`、`_ShellCommand`、`_CustomCommand`。
- 将 `_CustomCommandSpec` 的 name/prepare/metadata 迁移到 `_CustomCommand`。
- 将 Shell fallback 迁移到 `_ShellCommand`。
- 让 `_ExecutionRoute` 持有 Command 和 `parallel_safe`。
- 暂时保留旧调度行为所需的测试适配，仅在未提交代码中完成迁移。

验收：普通 Shell、`cd`、`export` 的行为不变，所有执行仍共享现有 Execution
lifecycle。

### 阶段 2：拆分 handlers 目录

- 创建 `handlers/`。
- 迁移 base contracts、Inline/Process executions、Shell handler。
- 将 `_prepare_cd` 和 `_prepare_export` 分别迁移到 `cd.py`、`export.py`。
- 将 `_DriverContext`、`_DriverExecution` 等名称替换为 Command/Prepared
  Execution 语义。
- 删除旧 `drivers/` import 并移除空目录。

验收：Ruff、mypy 和现有 Driver/Execution tests 全部使用新路径通过。

### 阶段 3：Parser、Policy、Approval 解耦

- 删除 `CommandParseResult.tool` 和 parser 对 `ToolCommand` 的导入。
- 将 `classify_tool_command` 改为纯 Tool facts parser。
- 删除 Kernel 的提前 Tool enrichment。
- 删除 Policy 的 Tool special case 和 `tool.*` rule。
- 删除 `ExecutionApprovalRequest.tool`。
- 更新 `runtime` 公共导出和相关测试。

验收：Policy、Approval 和自定义 Policy 只收到纯语法 CommandParseResult；拒绝和
审批仍在 route/admission 之前生效。

### 阶段 4：Tools 迁移为 Custom command

- 将 Tool handler 迁移到 `handlers/tools.py`。
- 默认 Registry 注册 `tools`。
- Kernel 将 Tool Catalog、Tool Environment 注入 ToolsCommand。
- 完成 `tools list/info/run` 的 custom route。
- 覆盖 invalid syntax、pipeline、redirection、malformed quote 不落 Shell 的场景。
- 保留 private venv、fresh worker、cancel、output 和 environment failure 行为。

验收：所有 Tools 能力测试通过，并能证明 `tools` 不再经过 special Tool route。

### 阶段 5：Tool parallel-safe metadata

- 为 `ToolEntry` 增加 `parallel_safe`。
- 在 Catalog AST inspection 中读取 `PARALLEL_SAFE`。
- 增加缺省、有效、非法、多次声明和 Workspace override 测试。
- 更新 index/info/system message 的 parallel-safe 展示。
- 删除 `parallel_tools` 的 Runtime、Kernel、Router、测试和文档参数。

验收：Tool 的并行资格完全由有效 Catalog facts 和静态 grammar facts 决定；动态
引用和明确为非 parallel-safe 的 Tool 始终串行，解析失败会回退到对应默认值并
产生 RuntimeDiagnostic。

### 阶段 6：移除 DriverKind 和 Lane

- 删除 `_DriverKind`、`_ExecutionLane`、route.lane。
- Scheduler 改为单队列、单 parallel limit 和 serial barrier。
- 删除 `tool_parallel_limit`、`_claim_lane`、`_lane_limit`。
- 更新 Supervisor 使用 `command.isolated` 和 `route.parallel_safe`。
- 更新 Execution snapshot、诊断字段和所有测试中的 lane/driver assertions。

验收：

- 连续 parallel-safe 命令可以并行；
- serial command 是全局 barrier；
- 后续 parallel-safe 命令不会越过 serial barrier；
- 不同 Session 仍可并行；
- close、kill、queue-full 和 preparation failure 语义不变。

### 阶段 7：文档、公共表面与回归

- 更新 `docs/architecture.md` 中的 Parser、Router、Scheduler、Handlers 和
  Tools 关系。
- 更新 RFC-0003 的 superseded sections 或增加迁移说明。
- 更新 `docs/discussions/aep-aligned-custom-dispatch-and-parallel-scheduling.md`
  和相关 scheduler discussion。
- 更新 system message 中对 Tools 并行声明的说明。
- 删除旧 symbol、旧目录和旧配置文档。
- 运行完整 pytest、Ruff、mypy 和 diff check。

## 测试计划

### Parser 与 Policy

- `CommandParseResult` 不包含 `tool` 属性。
- Parser 不导入 Tool facts。
- Policy 对 `tools list`、`tools run` 只使用通用 executable/syntax facts。
- `tools` 明确 deny/ask 时分别返回 deny/approval。
- Approval Request 不包含 Tool-specific 字段。
- policy failure 仍 fail closed。

### Registry 与 Route

- `cd`、`export`、`tools` 命中 Custom。
- 普通命令命中 Shell fallback。
- `./tools`、`/bin/tools`、`toolsmith` 不命中 `tools`。
- pipeline、redirection、非法参数和 malformed quote 命中 `tools` 后失败，
  不创建 Shell process。
- 保留命名冲突会失败。

### Tool Catalog

- 无声明默认为串行。
- `PARALLEL_SAFE = True` 被记录为 true。
- `PARALLEL_SAFE = False` 被记录为 false。
- 非字面量、非布尔值和重复声明产生 validation error。
- Workspace override 使用 effective file 的声明。
- index/info 展示声明，但修改 index 不能改变实际事实。

### Scheduler

- 两个 parallel-safe Shell command 并行执行。
- 两个 parallel-safe Tool command 并行执行。
- Tool 和 Shell 使用同一个 `parallel_limit`。
- serial command 等待所有更早运行项完成。
- serial barrier 后的 parallel-safe command 不越过 barrier。
- queued kill、running kill、close、queue full 和 preparation failure 正常。
- 同一 Session 的 `cd`、`export` 不并行执行。
- 不同 Session 仍可并行执行。

### Isolation 与生命周期

- `cd` 成功后影响后续 Session command 的 cwd。
- `export` 成功后影响后续 Session command 的 environment。
- parallel-safe command 使用 cwd/environment snapshot。
- Tools worker 不能修改 Session cwd 或 environment。
- Tool worker cancellation 仍终止 process group。
- Runtime close 和 Session close 清理所有 prepared/running execution。

### 静态回归检查

- `rg` 不再发现 `_DriverKind`、`_ExecutionLane`、`parallel_tools`、
  `tool_parallel_limit`、`command.tool`、`_ToolDriver` 的生产代码引用。
- `drivers/` 不再存在。
- 公共 `cli_agent.runtime` 不再导出 `ToolCommand`。
- 现有 model-visible syscall schema 仍只有 `exec`、`output`、`kill`。

## 失败与回滚策略

本次重构不保留旧 API 兼容层。实现按阶段合并前，先通过每个阶段的独立测试；
若某阶段失败，回滚该阶段未合并的 patch，而不是同时保留两套路由模型。

如果发现 Tool metadata 不能满足实际调度需求，优先将该 Tool 降级为 serial，
不自动恢复 Tool lane。若未来需要 Host 对 parallel-safe 进行额外限制，可在新的
RFC 中增加统一并行预算或 Policy constraint，不恢复 `_ExecutionLane`。

## 验收标准

- [ ] `CommandParseResult` 只包含通用语法事实。
- [ ] Policy 和 `ExecutionApprovalRequest` 不再依赖 Tool facts。
- [ ] `tools`、`cd`、`export` 都通过 Custom registry 进入执行链。
- [ ] 任意非法 reserved tools command 不会落到 Shell。
- [ ] `_DriverKind` 和 `_ExecutionLane` 已删除。
- [ ] Scheduler 只依赖 `parallel_safe` 和单一 `parallel_limit`。
- [ ] `ToolEntry` 能从有效 Tool 文件读取 `PARALLEL_SAFE`。
- [ ] Tool 无声明、动态引用或不完整 facts 时不会并行。
- [ ] Command `isolated` 能正确控制 Session context。
- [ ] Tool fresh worker、cancel、output、close 语义不回归。
- [ ] `parallel_tools` 和 `tool_parallel_limit` 已从 Runtime API、Kernel 和文档中删除。
- [ ] 完整 pytest、Ruff、mypy 和 diff check 通过。
- [ ] RFC-0003 的过时 Tool lane 设计已标记为 superseded。

## 开放问题

本 RFC 的默认决策已经确定；实现前只需在 Review 中确认以下命名细节：

1. `handlers/` 是否作为 `drivers/` 的最终替代目录名。
2. Tool metadata 是否统一使用 `PARALLEL_SAFE`，而不提供 companion markdown
   或 YAML manifest 写法。
3. `_Command` 是否使用 `ABC`，还是使用等价的 private Protocol；两者都不改变
   本 RFC 的运行时语义。

若没有新的反例，默认按本文建议的 `handlers/`、`PARALLEL_SAFE` 和 `ABC` 实施。
