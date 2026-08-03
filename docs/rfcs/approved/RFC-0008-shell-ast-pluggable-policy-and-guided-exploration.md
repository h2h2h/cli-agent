---
rfc_id: RFC-0008
title: Shell AST Parsing, Pluggable Execution Policy, and Guided Exploration
status: COMPLETED
author: cli-agent maintainers
reviewers:
  - name: project owner
    status: approved
created: 2026-08-03
last_updated: 2026-08-03
decision_date: 2026-08-03
related_prds: []
related_rfcs:
  - RFC-0001-host-mediated-execution-approval.md
  - RFC-0002-workspace-capability-view.md
  - ../proposed/RFC-0006-explicit-runtime-resource-ownership.md
  - ../proposed/RFC-0007-unified-command-routing-and-execution-refactor.md
---

# RFC-0008: Shell AST Parsing, Pluggable Execution Policy, and Guided Exploration

## 概述

本 RFC 将 Shell command 的语法解析、路由、可选授权、调度和执行拆成单向、
可独立演进的阶段：`parse_shell_ast` 只产生不可变的 Shell 语法树；Router 只选择
Custom command 或 Shell fallback 并确定调度属性；Host 可以选择注入一个
`ExecutionPolicy`，也可以完全跳过 Policy；只有 Policy 返回 `ASK` 时，Kernel 才通过
始终存在的通用 `UserInteraction` 通道向用户提问。

该设计不引入全局 Shell command Catalog。Scheduler 需要的 `parallel_safe` 由具体
Command 根据 AST 和现有显式配置计算，Capability View 需要的重定向和变更目标由它
自己消费 AST 并维护私有规则。AST 是共享的语法事实，不是共享的命令语义数据库。

同时，本 RFC 将来自 `Codex_Read_Tool_Design_Research.pdf` 的文件探索和读取建议整理为
静态 system message 指引，鼓励模型先搜索、再局部读取、收窄输出并并行执行彼此独立
的只读探索。本 RFC 不新增模型可见 syscall；模型仍只使用 `exec`、`output` 和
`kill`。

## 背景与上下文

### 起点与已验证方向

本轮重构以 commit `a2ea25d18ea342b3e2890c9dbcdaa13a96ce965b` 为语义起点。
后续实验验证了一个值得保留的方向：基于 `tree-sitter-bash` 的 AST parser 能让
pipeline、逻辑操作符、重定向、subshell 和 command substitution 由同一个语法边界
表达，而不需要各层重复扫描原始字符串。

实验也暴露了两个不应继续保留的方向：

1. `ShellParseResult` 被包装进 `PolicyEvaluation`，导致 Router、Supervisor 和
   Scheduler 通过授权对象间接取得命令，语法事实与授权结果无法独立流动。
2. 为共享命令语义而引入的 `shell_catalog` 反而让 Policy、调度和 Capability View
   依赖同一个不断扩张的分类层。该层同时承担语法、授权、并行和副作用推断，形成新的
   中央耦合点。

### 当前相关契约

RFC-0001 已确定 Host-mediated approval：Policy 可以返回 `ASK`，Host 决定是否对
当前命令放行。其实现契约还规定了默认 Policy、专用 Approver、Approval Request、
`PolicyEvaluation` 携带 parse result，以及最终 `ExecutionDecision` 进入路由。

RFC-0007 则把统一 Command 抽象和 custom-first routing 作为目标，但其流程仍将
Policy 放在 Router 之前，并让 Router 接收最终 `ExecutionDecision`。

本 RFC 保留以下原则：

- 只有 Host 才能提供与用户交互的能力；Policy 不直接操作终端或 UI。
- `ASK` 未成功解析为 allow-once 时不得创建 Execution。
- Custom command 和 Shell fallback 共享同一条调度、执行、取消和结果链路。

本 RFC 修订以下实现契约：

- Policy 从 Runtime 的必选默认组件改为可选插件。
- Router 在 Policy 之前运行，并只接收解析结果。
- `PolicyEvaluation` 不再携带 `ShellParseResult`。
- 删除 `ExecutionDecision`、专用 Approver 及 Approval Request 类型。
- 使用通用且始终存在的 `UserInteraction` 取代仅服务于 Policy 的 Approver。

本 RFC 获批后，上述条目在冲突范围内取代 RFC-0001 与 RFC-0007 的既有描述；其余
目标和约束继续有效。

### 术语

| 术语 | 定义 |
|---|---|
| Shell AST | `tree-sitter-bash` 解析后形成的不可变 Shell 语法树及来源位置。 |
| Parsed Command | 一次成功解析得到的 `ShellParseResult`；它是语法事实，不是授权结论。 |
| Route | Router 选出的具体 Command 与 Runtime-trusted `parallel_safe`。 |
| Execution Policy | Host 可选注入的异步授权插件，对 Parsed Command 返回 `ALLOW`、`ASK` 或 `DENY`。 |
| User Interaction | Host 必须提供的通用异步提问通道，可返回自由文本或预定义选项。 |
| Admission | Supervisor 在路由和可选授权完成后，将 command 与 route 交给 Scheduler 的动作。 |

## 问题陈述

### 需要解决的问题

当前设计把“命令是什么”“是否允许执行”和“如何执行”串在同一对象流中。Policy
产生携带 parse result 的 evaluation，approval 再产生 final decision，Router 最后
从 decision 中取回命令。这意味着即使 Host 不需要 Policy，主链路仍必须构造和传播
授权对象；Router 也无法作为独立的纯函数被测试或复用。

另一种尝试是用 Shell Catalog 统一每个命令的风险、并行和副作用事实。但这些事实的
所有者并不相同：Policy 关心授权，Command 关心调度安全，Capability View 关心
copy-up 和重定向。把它们放入一个 Catalog 会造成跨层依赖，并诱使系统把不完整的
命令名分类误当成安全边界。

最后，专用 Approver 把 Host 与用户交互窄化为 Policy approval。未来 Runtime
内部可能需要让用户从若干方案中选择，或收集自由文本。如果每种场景都增加一个专用
回调，Host 集成和生命周期管理会不断重复。

### 不处理的影响

- `policy=None` 仍无法真正跳过 Policy，嵌入式 Host 必须接受隐式授权策略。
- Parser、Policy、Router 和 Supervisor 继续通过复合对象互相了解实现细节。
- Catalog 会成为新增 Shell 语义时必须同步修改的中央注册表。
- 用户提问能力继续绑定 approval，无法作为通用 Host 能力复用。
- malformed Shell command 可能绕过 parser 后进入 custom routing 或执行，失败边界
  不稳定。

## 目标与非目标

### 目标

1. 使用 `parse_shell_ast` 为每个 `exec` 建立唯一、不可变的 Shell 语法事实。
2. 解析失败时立即返回 `invalid_argument` ToolResult，不进入 Router、Policy、
   Supervisor 或执行阶段。
3. 让 Router 只依赖 `ShellParseResult`，并纯粹地产生 Command route。
4. 让 `ExecutionPolicy` 成为 Runtime open 时可选注入的单个插件；未注入时完全跳过。
5. 让 Policy 对所有已路由的 Custom 和 Shell command 生效，但输入仍只有 Parsed
   Command，不依赖 route、cwd 或 Session context。
6. 以 Host-owned、Runtime-wide、始终存在的 `UserInteraction` 支持 ASK 和未来的
   通用用户提问。
7. 保留统一 Scheduler、Execution、output、kill 和 close 语义。
8. 让调度和 Capability View 直接消费 AST 中各自需要的事实，不引入 Catalog。
9. 将 Shell 探索与文件读取的高价值工作流写入静态 system message。
10. 不保留旧 Policy、Approver 和 Decision 公共 API 的兼容层。

### 非目标

1. 实现任何内置 Policy 策略、黑白名单、规则 DSL 或多 Policy chain。
2. 建立 Shell command Catalog、通用副作用分类器或安全命令清单。
3. 证明一个命令无副作用，或提供操作系统级 sandbox。
4. 新增模型可见的通用 question syscall。
5. 解决同一 Runtime 多 Session 并发提问的排队、公平性或 UI 复用问题。
6. 为 user interaction 设置 Runtime 固定超时。
7. 固定 Shell 执行器为 Bash，或配置 parser/executor 的 Shell dialect。
8. 让 Policy 重写、替换或规范化 Parsed Command。
9. 动态发现、热加载或组合多个 Policy。

## 设计原则

### 共享语法，不共享推断

AST 统一“命令如何组成”的事实；各消费者只在自己的边界内解释事实：

- Router 识别 Custom command 或选择 Shell fallback。
- Command 计算 `parallel_safe`。
- Policy 决定是否授权。
- Capability View 识别它负责准备的路径。

没有任何消费者可以把自己的派生结论写回 AST，也不建立跨消费者共享的
`ShellEffect`、Composite Facts 或 Catalog entry。

### 路由不是授权

Router 可以在没有 Policy 的 Runtime 中独立工作。Router 选择“由谁执行”，Policy
选择“本次是否允许执行”。先路由不会创建 Execution、占用 Scheduler 容量或产生
副作用，因此不构成提前执行。

### ASK 是提问的一种来源

Policy 只返回结构化的 `ASK` 结论，不持有 UI。Kernel 将它转换为标准
`UserQuestion`；Host 决定如何显示和回答。`UserInteraction` 的抽象不包含 approval
语义，因此未来可以服务于其他 Runtime-owned 提问，但本 RFC 不暴露新的模型接口。

### 失败关闭当前命令，而不是终止 Session

Parser 失败、Policy 异常、非法 Policy 返回值、用户取消、交互异常和非法回答都必须
阻止当前命令执行。除正常的 close/cancel 外，这些失败只产生一个 ToolResult；Agent
Session 继续可用。

## 方案比较

### 方案 A：只替换 AST parser，保留 mandatory Policy 和 ExecutionDecision

该方案迁移成本最低，也保留 RFC-0001 的现有对象链。缺点是无 Policy 的 Host 仍要
经过隐式 Policy，Router 继续依赖授权对象，parser 与主链路的解耦并未完成。

### 方案 B：使用 Shell Catalog 汇总共享命令语义

该方案让 Policy、Scheduler 和 Capability View 读取相同 facts，看起来能减少重复。
但授权、并行与 copy-up 的规则变化频率和可信边界不同；Catalog 会成为跨层中央依赖，
并可能把按 executable name 的启发式判断包装成过强的安全语义。

### 方案 C：AST + 纯 Router + 可选 Policy + 通用 UserInteraction

该方案让主链路只传播 Parsed Command 和 Route，Policy 作为独立插件旁接在 admission
之前。代价是需要修订已批准的 approval API，并允许少量面向具体消费者的 AST 解释
规则存在于不同模块。但这些规则承担不同职责，这种局部重复比共享错误抽象更容易
审查和测试。

### 评估矩阵

评分为 1（差）到 5（好），加权总分满分 5。

| 评估项 | 权重 | 方案 A | 方案 B | 方案 C |
|---|---:|---:|---:|---:|
| 主链路解耦 | 30% | 2 | 2 | 5 |
| 组件边界清晰度 | 25% | 3 | 1 | 5 |
| 语法一致性 | 20% | 5 | 5 | 5 |
| Host 交互扩展性 | 15% | 2 | 2 | 5 |
| 实施与迁移成本 | 10% | 5 | 2 | 3 |
| 加权总分 | 100% | 3.15 | 2.35 | 4.80 |

本 RFC 选择方案 C。它增加一次明确的公共 API 迁移，但能移除长期的控制流和中央
语义耦合。

## 详细设计

### 执行主链路

```text
exec(raw command)
  -> parse_shell_ast
     -> parse failure: invalid_argument ToolResult
  -> Router.resolve(parsed command)
     -> Custom command or Shell fallback + parallel_safe
  -> optional ExecutionPolicy.evaluate(parsed command)
     -> ALLOW: continue
     -> DENY: policy_denied ToolResult
     -> ASK: UserInteraction.ask(standard question)
        -> allow_once: continue
        -> deny / cancel / invalid / failure: policy_denied ToolResult
  -> Supervisor.admit(parsed command, route)
  -> Scheduler
  -> Execution
```

Router 与 Policy 都在 Scheduler admission 之前运行。它们不创建 Handle，不占用并发
容量，也不启动子进程。现有 batch preflight 保持顺序执行，因此在本 RFC 的单 Runtime、
单 Session 运行假设下，同一时刻最多处理一个用户问题。

### Shell AST parser

公开的解析入口改为：

```python
def parse_shell_ast(command: str) -> ShellParseResult:
    ...
```

`ShellParseResult` 是不可变数据，至少保留原始 command、根节点、节点类型、token、
source span 和解析错误所需事实。parser 使用 `tree-sitter-bash` 表达 simple command、
pipeline、`&&`、`||`、`;`、重定向、后台执行、subshell 和 command substitution。

以下情况视为 parse failure：

- parser 未产生 root；
- 输入为空或没有可执行的语法节点；
- AST 含 syntax error 或 missing node。

Kernel 对这些情况返回：

```text
code: invalid_argument
message: invalid shell command
```

Parse failure 不进入 Router，因此 malformed custom command 也不会再由 Custom handler
处理。这是有意移除旧行为，而不是兼容性缺陷。

不能被 Runtime 进一步解释的合法 AST 由 `UnsupportedCommand` 等节点表达，仍属于
parse success，可进入 Custom 或 Shell fallback。parser 成功只表示能构建 AST，不
保证底层 Shell 一定接受或成功执行。

Shell executor 继续使用 `asyncio.create_subprocess_shell()` 的平台默认 `/bin/sh`。
本 RFC 不把 executor 固定为 Bash，也不承诺 tree-sitter-bash 与 `/bin/sh` 的语法
完全等价；执行期 dialect 差异继续以普通 Shell 失败返回。

### 纯 Router 契约

Router 的核心契约为：

```python
def resolve(command: ShellParseResult) -> _ExecutionRoute:
    ...
```

`_ExecutionRoute` 包含选中的 `_Command` 和 Runtime-trusted `parallel_safe`。Router：

- 按现有 custom-first 规则选择 Custom command，否则选择 Shell fallback；
- 读取 Command 自身的静态元数据和 AST 事实计算 `parallel_safe`；
- 保留显式 `parallel_commands` 配置的现有含义；
- 不调用 Policy、UserInteraction、Scheduler 或 Execution；
- 不接收或返回 `PolicyEvaluation`、`PolicyAction` 或 `ExecutionDecision`。

### 可选 Execution Policy

Policy 只有最小插件协议：

```python
class ExecutionPolicy(Protocol):
    async def evaluate(
        self,
        command: ShellParseResult,
    ) -> PolicyEvaluation: ...


@dataclass(frozen=True)
class PolicyEvaluation:
    action: PolicyAction
    rule_id: str
    reason: str | None = None
```

`PolicyAction` 保留 `ALLOW`、`ASK` 和 `DENY`。不增加多 Policy chain 所需的
`CONTINUE`。`PolicyEvaluation` 不包含 command；Kernel 继续持有原始不可变
`ShellParseResult`。

`AgentRuntime.open` 在 Runtime 生命周期开始时接收零个或一个 Policy：

```python
await AgentRuntime.open(
    provider=provider,
    user_interaction=user_interaction,
    execution_policy=None,
)
```

- `execution_policy=None` 表示完全跳过 Policy，不构造默认 Policy，也不产生隐式
  allow decision。
- 非 `None` Policy 对每个成功解析且成功路由的 Custom command 和 Shell fallback
  调用一次。
- Policy 的输入不包含 route、cwd、Session context 或 Host UI。
- Policy 实例固定到 Runtime 生命周期；不支持动态发现、替换、热加载或多个 Policy。
- Runtime 不要求 Policy 提供 close；资源所有权仍由创建它的 Host 管理。

本 RFC 不实现任何具体 Policy。命令黑白名单、风险策略和规则配置需要在后续 RFC 中
单独定义其语义、组合规则和误判边界。

### 通用 UserInteraction

公共交互协议为：

```python
@dataclass(frozen=True)
class UserOption:
    value: str
    label: str


@dataclass(frozen=True)
class UserQuestion:
    request_id: str
    session_id: str
    prompt: str
    options: tuple[UserOption, ...] = ()


@dataclass(frozen=True)
class UserAnswer:
    value: str | None


class UserInteraction(Protocol):
    async def ask(self, request: UserQuestion) -> UserAnswer: ...
```

当 `options` 为空时，`value` 是自由文本；当 `options` 非空时，非空 `value` 必须匹配
一个 option value。`None` 表示用户取消或当前无法回答。

`user_interaction` 是 `AgentRuntime.open` 的必选依赖，即使 `execution_policy=None` 也
必须提供。它由 Host 创建并拥有，Runtime-wide 共享，Runtime close 不关闭它。
Reference CLI 提供终端实现；嵌入式 Host 可以提供 GUI、远端或不可交互实现。

本 RFC 只定义 ASK 的一个调用点。Kernel 将 `PolicyEvaluation` 转为标准问题：

- prompt 包含 Policy reason 和原始 command；
- options 固定为 `allow_once` 与 `deny`；
- `allow_once` 只放行当前这一个 parsed command，不持久化；
- `deny`、`None` 或非法 value 都阻止当前命令。

Policy 不接收 `UserInteraction`，也不能直接提问。模型不可直接构造 `UserQuestion`。

第一版不设置 Runtime 固定 timeout。Session close 取消属于该 Session 的 pending ask
task；Runtime close 取消全部 pending ask task。取消不关闭 Host 的 interaction 对象。
本 RFC 默认一个 Runtime 实际只服务一个 Session，不删除现有多 Session API，但不对
并发提问的顺序、公平性或 UI 行为作出保证。

### Policy 与交互失败语义

| 场景 | 当前 exec 结果 | Diagnostic | Session |
|---|---|---|---|
| Policy 返回 `DENY` | `policy_denied`，使用 Policy reason | 可选 | 继续可用 |
| Policy 抛出异常 | `policy_denied`，通用消息 | 记录内部异常 | 继续可用 |
| Policy 返回非法对象或 action | `policy_denied`，通用消息 | 记录非法返回 | 继续可用 |
| ASK 返回 `deny` | `policy_denied`，使用 Policy reason | 不要求 | 继续可用 |
| ASK 返回 `None` | `policy_denied`，通用消息 | 可选 | 继续可用 |
| ASK 抛出异常 | `policy_denied`，通用消息 | 记录内部异常 | 继续可用 |
| ASK 返回非法 option | `policy_denied`，通用消息 | 记录非法回答 | 继续可用 |

所有 Policy 相关失败沿用现有 `policy_denied` code，不增加新的模型可见错误码。异常
详情只进入 Host diagnostic，不暴露给模型。正常 deny 可以把经过 Policy 选择的
`reason` 返回模型。

### Supervisor、Scheduler 与 Execution

删除 `ExecutionDecision` 后，Supervisor 的 admission 接收原始 Parsed Command 和
Route。Policy metadata 不进入 Scheduler 或 Execution contract。

Scheduler 继续只消费 Runtime-trusted `parallel_safe`，并保留现有有序 barrier、
批次结果顺序和取消语义。Shell Command 从 AST 判断 composition 与 executable，并
结合显式 `parallel_commands` 计算并行安全；Custom Command 继续拥有自己的元数据。
这里不引入 Catalog 或通用 effect facts。

### Capability View

Capability View 的 Shell 准备入口改为接收 AST：

```python
def prepare_shell(command: ShellParseResult) -> PreparedCapability: ...
```

输出重定向目标从 AST redirect 节点取得，不再扫描原始字符串。`cp`、`mv`、`rm`、
`sed` 等命令的 mutation target 规则仍是 Capability View 的私有实现，因为这些规则
只服务于 copy-up、whiteout 和 materialization。它们不导出为全局 `ShellEffect`，也
不供 Policy 或 Scheduler 读取。

Parse failure 永远不会到达 Capability View。Custom command 的状态准备继续由各自
handler 负责。

### 静态 Shell 探索指引

system message 增加简短的静态工作流，而不是从 Catalog 动态生成命令说明：

1. 在读取大量文件前，先用文件名搜索和文本搜索定位候选范围。
2. 优先用 Shell 中可审查、可收窄的读取方式，避免为了普通读取编写临时 Python
   dump 脚本。
3. 使用行范围、匹配上下文、字段选择或结果数量限制缩小输出。
4. 对彼此独立的只读探索使用同一批次并行调用；存在依赖关系时保持顺序。
5. 在修改前先确认目标、上下文和读写边界，再选择具体变更方式。

该文字参考 `docs/references/Codex_Read_Tool_Design_Research.pdf` 的第 6、7 节与附录 B，
只提取当前项目已有能力可以兑现的原则。不宣称不存在的专用 read tool、`apply_patch`
tool 或 workdir 参数，也不把这些建议描述为安全保证。

### 公共 API 清理

本项目不保留向后兼容层。实施时：

- 删除 `ExecutablePolicy`；
- 删除 `ExecutionApprover`；
- 删除 `ExecutionApprovalRequest`；
- 删除 `ApprovalResponse`；
- 删除 `_ExecutionApprovalGate`；
- 删除 `ExecutionDecision`；
- 保留 `ExecutionPolicy`、`PolicyEvaluation` 和 `PolicyAction`；
- 新增 `UserInteraction`、`UserQuestion`、`UserOption` 和 `UserAnswer`；
- 更新所有 Runtime factory、Reference CLI、测试与文档调用点。

## 数据流与所有权

```text
Host
  ├─ owns Provider
  ├─ owns UserInteraction (required)
  └─ optionally owns ExecutionPolicy
         │
         ▼
AgentRuntime
  └─ Session Kernel
       ├─ parse_shell_ast(raw) ───────────────┐
       ├─ Router.resolve(parsed) -> route     │ immutable parsed command
       ├─ Policy.evaluate(parsed), optional ─┤
       ├─ UserInteraction.ask(...), on ASK   │
       └─ Supervisor.admit(parsed, route) ◄──┘
              └─ Scheduler -> Execution
```

Runtime 只持有 Host 资源的借用引用，不关闭 Provider、Policy 或 UserInteraction。Session
与 Runtime close 只取消其内部正在等待的任务。

## 安全与隐私

- `execution_policy=None` 明确表示 Host 没有配置 Runtime 授权检查。Runtime 不应把
  AST parsing、Router 或 Capability View 描述为替代 Policy 的安全边界。
- Parser 只能验证语法结构，不能证明命令安全、无副作用或可由 `/bin/sh` 成功执行。
- Policy 的 command-name 或 AST 启发式判断仍可能被 wrapper、动态代码、脚本和运行期
  输入绕过；具体策略必须自行说明限制。
- ASK prompt 会包含原始 command，可能带有凭据或敏感路径。UserInteraction 实现不得
  默认把问题发送给未经 Host 授权的第三方，也不应由 Runtime 自动持久化回答。
- Policy 和 interaction 的异常细节只进入 Host diagnostic，避免把内部堆栈、规则或
  用户界面信息泄露给模型。
- Capability View 仍是工作区视图与写入准备机制，不是 OS sandbox。

## 兼容性与迁移

### 行为变化

- malformed Shell command 从可能进入 custom handler 或 Shell execution 改为立即返回
  `invalid_argument`。
- 未配置 Policy 时，从默认 Policy 行为改为完全不做 Policy evaluation。
- 配置 Policy 时，Router 会先解析 route，但仍在任何 admission 或执行之前完成授权。
- ASK 从专用 approval callback 改为通用 UserInteraction question。
- Shell executor 仍使用当前平台默认 `/bin/sh`，不因 AST parser 改为 Bash。

### 迁移步骤

1. 从基线恢复主链路，保留 AST parser 实现并完成 parse-failure 边界。
2. 让 Router 直接消费 `ShellParseResult` 并产出 Route，删除 Decision 依赖。
3. 引入可选 `ExecutionPolicy`，统一 Custom 与 Shell fallback 的 evaluation。
4. 引入必选 `UserInteraction`，迁移 Reference CLI 的 ASK UI。
5. 收敛 Shell Command 与 Capability View 对 AST 的职责，不引入 Catalog 或
   composite facts。
6. 增加静态探索指引与跨层 contract tests。
7. 更新架构、discussion、README 和公共导出，删除旧术语。

没有持久化数据或网络协议需要迁移。因为项目明确不保留向后兼容，旧 Host 集成必须在
同一版本中改为提供 `user_interaction`，并按需传入 `execution_policy`。

## 测试策略

### Parser contract

- 覆盖 simple command、组合操作符、重定向、subshell、command substitution 与来源
  位置。
- root 缺失、空输入、error node 和 missing node 都返回稳定 parse failure。
- 合法但 Runtime 未解释的语法仍可进入 Shell fallback。

### 路由与 Policy contract

- Router 只接收 Parsed Command，且不产生副作用。
- `policy=None` 时不发生 evaluate 或 approval 对象构造。
- 配置 Policy 时，Custom 与 Shell fallback 都各 evaluate 一次。
- ALLOW、DENY、ASK、异常和非法结果均符合表格中的 ToolResult 与 diagnostic 语义。
- 被阻止的命令不产生 Handle、Scheduler item 或 Execution。

### UserInteraction contract

- options 为空时接受自由文本；非空时只接受声明的 value。
- ASK 只接受 `allow_once`，`deny`、`None` 和非法值 fail closed。
- Session/Runtime close 会取消 pending ask，但不关闭 interaction。

### 下游消费者

- 显式 `parallel_commands`、Shell composition 与 Custom metadata 继续产生预期的
  `parallel_safe`。
- Capability View 从 AST redirects 取得目标，私有 mutation 规则覆盖已有命令。
- system message 包含搜索优先、收窄输出、读写边界和独立读取并行化指引。
- 公共导出与文档中不再出现已删除的 approval/decision API。

## 发布、观测与回滚

该变更作为一个不保留兼容层的版本整体发布，不设置 feature flag，也不同时维护新旧
链路。Runtime diagnostic 应能区分 parser、Policy 和 UserInteraction 失败来源，但
模型可见错误码保持稳定。

如需在发布前回滚，应整体 revert M17 merge。实现一旦发布，回滚必须连同 Host API、
Reference CLI 和测试一起回滚，不能只恢复 mandatory Policy 或旧 Approver，否则会
重新产生两套不一致契约。

## 风险与缓解

| 风险 | 影响 | 缓解措施 |
|---|---|---|
| tree-sitter-bash 接受而 `/bin/sh` 拒绝 | 命令在执行期失败 | 明确 parser 只验证 AST；保留 Shell 原始退出结果。 |
| 可选 Policy 被误解为默认安全 | Host 未配置授权保护 | API 与文档明确 `None` 的含义；不提供隐式默认策略。 |
| AST 私有解释规则出现少量重复 | 不同模块维护相似 executable 判断 | 保持规则局部、目标单一，用跨层测试验证而不引入共享 Catalog。 |
| 通用交互抽象过早扩张 | 增加未使用的复杂度 | 第一版只支持 `ask`、文本值和静态选项，不增加模型 syscall。 |
| 单 Session 假设限制未来并发 UI | 多 Session Host 行为未定义 | 明确记录为后续设计议题，不在本 RFC 中加入错误的队列抽象。 |
| 修订已批准 RFC-0001 API | Host 与文档迁移成本 | 一次性删除旧类型，并以 contract tests 固化唯一新路径。 |

## 未决与延期议题

以下议题不阻塞本 RFC，但需要独立设计后才能实现：

1. 内置 Policy 是否需要命令规则、风险等级或持久化用户选择。
2. 多 Policy 的组合顺序、短路语义和 provenance。
3. 多 Session 并发提问的队列、公平性、取消与 request routing。
4. 是否向模型开放通用 question syscall，以及如何防止与 Host control plane 混淆。
5. 是否支持显式 Shell dialect 配置，并让 parser 与 executor 使用同一 dialect。

## 决策记录

| 日期 | 状态 | 说明 |
|---|---|---|
| 2026-08-03 | PROPOSED | 放弃 Shell Catalog 方向，提出 AST、纯 Router、可选 Policy 与通用 UserInteraction 的解耦方案。 |
| 2026-08-03 | COMPLETED | project owner 完成 peer review；M17 全部 issues 已实施并通过验证。 |

project owner 已批准本 RFC，并确认 M17 实现通过 peer review。所有相关 issue 均标记为
`resolved`。

## 参考资料

- [RFC-0001: Host-mediated execution approval](RFC-0001-host-mediated-execution-approval.md)
- [RFC-0002: Workspace Capability View](RFC-0002-workspace-capability-view.md)
- [RFC-0006: Explicit Runtime Resource Ownership](../proposed/RFC-0006-explicit-runtime-resource-ownership.md)
- [RFC-0007: Unified Command Routing and Execution Refactor](../proposed/RFC-0007-unified-command-routing-and-execution-refactor.md)
- [Codex Read Tool Design Research](../../references/Codex_Read_Tool_Design_Research.pdf)
