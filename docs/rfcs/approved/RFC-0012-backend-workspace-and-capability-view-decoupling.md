---
rfc_id: RFC-0012
title: Backend Workspace and Capability View Decoupling
status: APPROVED
author: cli-agent maintainers
reviewers:
  - name: project owner
    status: approved
created: 2026-08-06
last_updated: 2026-08-07
decision_date: 2026-08-06
related_prds: []
related_rfcs:
  - RFC-0002-workspace-capability-view.md
  - RFC-0003-tool-capability-commands.md
  - RFC-0005-mcp-tool-projection-and-bounded-bindings.md
  - RFC-0006-explicit-runtime-resource-ownership.md
  - RFC-0007-unified-command-routing-and-execution-refactor.md
  - RFC-0009-file-write-and-edit-commands.md
  - RFC-0011-non-blocking-model-generated-library-indexes.md
---

# RFC-0012：Backend Workspace 与 Capability View 解耦

## 概述

本 RFC 提议在 `AgentRuntime` 与 Session-scoped `EnvironmentKernel` 之间引入一个
Runtime-owned `BackendWorkspace`。它统一承载普通 Shell、Tool worker、文件操作和
Capability Catalog 所依赖的实际文件系统命名空间，并把 Local subprocess、Host
`Path`、symlink、copy-up、Tool venv 与未来 Sandbox/Remote 实现限制在 Backend
边界内。

推荐方案保留现有 Parser、Policy、Router、Scheduler、Execution Supervisor 与
`PreparedExecution` 合同。Command Handler 继续拥有命令语义，但不再创建 Host
进程或直接访问 Host 文件系统。首期不引入独立 `BackendSession`：每个 Kernel
继续拥有自己的 cwd 和环境状态，多个 Kernel 直接共享同一个
`BackendWorkspace`，一次 admitted command 仍只拥有一个 `PreparedExecution`。

本 RFC 不选择具体容器、VM 或 Remote provider，不定义 Remote 同步协议，也不
宣称 Local Backend 具有操作系统级隔离能力。它定义后续实现这些机制所必须遵守
的内部边界和生命周期。

## 背景与上下文

### 当前状态

当前模型可见 syscall 固定为 `exec`、`output` 和 `kill`。一个 `exec` 请求经过：

```text
parse -> route -> optional policy -> schedule -> handler
      -> PreparedExecution -> ExecutionSupervisor
      -> backend-neutral Execution Snapshot
```

`PreparedExecution.run(output)`、`cancel()`、`ExecutionOutcome`、有界输出、Cursor、
终态与 Session close 已经不依赖 Shell 或 Tool 类型。Supervisor 只运行和取消
Prepared Execution，不读取 subprocess、Tool worker 或文件操作细节。

执行机制与 Workspace 物理实现尚未解耦：

1. `_ShellHandler` 使用 `asyncio.create_subprocess_shell`，直接传入 Host cwd、
   Host 环境与本地 process-group 参数。
2. `_ToolHandler` 直接选择 Host venv Python、Runtime package 中的 worker 文件、
   Host Tool 路径，并使用 `asyncio.create_subprocess_exec`。
3. `_FileHandler` 使用 Host `Path` 完成目录创建、读取、原子替换与 Capability
   copy-up。
4. `cd` 虽然属于 Runtime-local Session 状态变更，却通过 Host
   `Path.exists()`/`is_dir()` 验证目标。
5. Tool、Skill、Library 与 MCP Catalog 直接遍历有效 View 的 Host Path；Library
   后台 worker 在 Runtime 活动期继续读取 source 和写入 `index.md`。
6. `_CapabilityView` 同时表达逻辑 lower/upper/whiteout 状态与 Local 文件级
   symlink/copy-up 物化机制。
7. `_ToolEnvironment` 在 Host `.workspace` 下创建 venv，并在 Host 上执行 `uv`。

因此，只把 `_ShellHandler` 的 subprocess 创建抽到一个 Backend，不能让 Sandbox
或 Remote Backend 工作。Handler、Catalog、`cd` 和 Tool worker 仍可能操作 Host
路径，而 Shell 实际运行在另一个文件系统命名空间中。

### 已有架构约束

本 RFC 继承以下决定：

- 一个 active Agent Session 拥有一个 `AgentLoop` 和一个
  `EnvironmentKernel`。
- 不同 Session 共享普通 Workspace 文件可见性，但 cwd、环境、Scheduler、
  Execution Handle 与上下文相互隔离。
- Parser 产生纯 Shell 语法事实；Policy、Router 和 Scheduler 不执行命令。
- Command Handler 返回统一 `PreparedExecution`；Supervisor 不按 Handler 或
  Backend 类型分支。
- Capability Catalog 的 provenance 必须来自 Runtime-trusted state，不能来自
  capability 文件中的自述元数据。
- Runtime open 失败不能静默扩大执行权限。
- 项目不保留旧内部 API 或旧物理布局的兼容层。

### 术语

| 术语 | 定义 |
|---|---|
| Backend | 配置并打开某种执行环境的 Runtime 内部机制，例如 Local、Sandbox 或 Remote。 |
| Backend Workspace | 一个已经打开的 live 执行工作空间；命令和文件操作在其中共享同一命名空间。 |
| Workspace Filesystem | Backend Workspace 提供的异步文件系统合同。 |
| Capability Source | Repertoire 等 capability lower 输入，不等同于 live View。 |
| Capability State | Workspace upper、whiteout 和 provenance 所需的持久逻辑状态。 |
| Bound Capability View | Capability Source/State 在某个 Backend Workspace 中绑定或物化后的有效 View。 |
| Backend path | Backend Workspace 内可解释的路径字符串，不假定为 Host `pathlib.Path`。 |
| Runtime-local Execution | 不启动 Backend 命令、但仍遵守 Prepared Execution 生命周期的 Runtime 操作。 |
| Backend-mediated Runtime-local Execution | 状态变更在 Runtime 内完成，但需要 Backend Filesystem 查询的操作，例如 `cd`。 |

## 问题陈述

### 需要解决的问题

当前代码把“命令在哪里运行”“文件在哪里读写”和“Capability View 如何物化”隐含
绑定到 Host 文件系统。新的 Backend 如果只替换 subprocess，会产生至少三类
命名空间不一致：

- Shell 在 Sandbox 中修改文件，File Handler 或 Catalog 仍从 Host 读取；
- Tool worker 在 Host 运行，读取不到 Sandbox 中的 cwd、Tools 或普通 Workspace
  文件；
- Host symlink/copy-up 先修改 Host View，随后 Remote Shell 在另一个 View 中执行。

Backend 类型继续渗透到每个 Handler 会形成另一种耦合：Shell、Tool、Files、
Catalog 和后续命令都需要分别增加 Local/Sandbox/Remote 分支，Supervisor 之外的
每一层都必须了解物理执行机制。

### 依据

- 当前 `_ShellHandler`、`_ToolHandler` 和 `_FileHandler` 分别直接拥有 Host
  subprocess 或 Host 文件操作。
- 当前 `_CapabilityView.root`、Catalog entry path 和 `_ToolEnvironment.python`
  都是 Host `Path`，并由 Kernel 直接注入 Handler。
- 当前 Runtime 可以同时拥有多个 Session；这些 Session 已约定共享 Workspace
  文件，而不共享 Session 状态。
- 当前 `PreparedExecution` 与 Supervisor 已能统一 process 和 inline execution，
  因此无需重写模型可见协议或 Execution lifecycle。
- OpenHands Workspace 把命令执行、文件操作和资源生命周期放在同一个 Workspace
  抽象中；Deep Agents Backend 也同时定义文件操作和可选 execute 能力。
- OpenAI Agents SDK 将 live sandbox session 定义为命令运行和文件变化所在的
  workspace，并与 conversation Session 区分。

### 不处理的影响

- 新增 Sandbox Backend 时仍需改动每个 Handler 与 Catalog。
- Local、Sandbox 和 Remote 的行为可能在路径、cwd、Tool、MCP 与文件可见性上
  静默分叉。
- Capability lower 的只读语义只能依赖 Local Shell mutation 启发式，无法由原生
  overlay Backend 实现更强保证。
- Host Provider 凭证等 ambient environment 会继续由 Handler 无条件带入子进程。
- Backend open、persist、close 与失败回收无法成为 Runtime 的明确生命周期。

## 目标与非目标

### 目标

1. 定义一个 Runtime-owned `BackendWorkspace`，使命令与文件操作使用同一个实际
   Workspace 命名空间。
2. 让 Shell、Tool、Files、`cd` 和 Capability Catalog 不依赖具体 Backend 类型。
3. 让 Command Handler 只完成命令语义到执行请求或 Runtime-local Execution 的
   转换。
4. 保留 `PreparedExecution`、Supervisor、Scheduler、Execution Snapshot、Cursor
   和 cancellation 合同。
5. 将 Capability Source/State 与 Bound Capability View 的物理呈现分离。
6. 让 symlink、copy-up、mount、snapshot、upload 与 Remote job handle 只存在于
   Backend 实现内部。
7. 保持一个 Runtime Workspace 被多个 Agent Session 共享的既有语义。
8. 明确 Backend open、Tool Environment reconcile、flush 与 close 的顺序。
9. Backend 初始化或约束应用失败时 fail closed，且不回退到 Local Backend。
10. 使用 Local Backend 证明当前行为可以在新边界上表达，再实现 Sandbox。

### 非目标

1. 选择 Docker、Podman、Firecracker、E2B、Modal 或任何 Remote provider。
2. 定义 Remote Workspace 的增量同步、冲突合并或断线恢复协议。
3. 为不同 Agent Session 创建互相隔离的 Workspace。
4. 动态刷新已打开 Runtime 的 Tool 或 Skill Catalog。
5. 更改模型可见 syscall 或 Execution Snapshot schema。
6. 把 Backend 类型暴露给模型或让 capability 文件选择 Backend。
7. 宣称 Local Backend、Policy 或路径规范化构成 OS sandbox。
8. 保留 Handler 构造参数、Host Path payload 或 `.workspace` 物理布局的兼容层。
9. 将 Runtime、AgentLoop、Context 管理或 Model Provider 移入 Remote Backend。
10. 在本 RFC 中发布第三方 Backend plugin API。

### 成功标准

- [ ] Runtime 执行代码中只有 Local Backend 可以创建 Host subprocess。
- [ ] Shell、Tool、Files 与 `cd` Handler 不导入或检查具体 Backend 类型。
- [ ] 非 Local Handler 与 Catalog 不使用 Host `Path` 访问 live Backend Workspace。
- [ ] 普通 Shell、Tool worker、Files 和 Catalog 通过测试证明看到同一文件变化。
- [ ] 两个 Session 继续共享 Workspace 文件，但保持独立 cwd、环境与 Handle。
- [ ] Supervisor、Scheduler、`exec`、`output`、`kill` 和 Snapshot schema 无
      Backend-specific 分支。
- [ ] Local、Sandbox 或 Remote Backend open 失败不会尝试权限更宽的替代 Backend。
- [ ] Runtime close 先停止 Session 与后台使用者，再 flush/close Backend Workspace。
- [ ] Local Backend 回归测试覆盖 Capability lower、upper、whiteout 和 copy-up。
- [ ] 一个最小 Sandbox contract test 证明文件、Shell 与 Tool 位于同一命名空间。

## 评估标准

以下标准使用 1–5 的定性评分，5 表示最充分满足当前要求。权重反映本次重构的
优先级，而不是对未来所有 Backend 的永久排序。

| 标准 | 权重 | 说明 | 最低要求 |
|---|---:|---|---|
| 命名空间一致性 | 25% | Shell、Tool、Files、Catalog 是否操作同一 Workspace | 不允许跨 Host/Backend 静默读写 |
| Handler 解耦 | 20% | 新增 Backend 时是否无需修改每个 Handler | Handler 不读取 Backend 类型 |
| 生命周期正确性 | 15% | open、Execution、flush、close 与失败回收是否有单一 owner | fail closed；close 顺序明确 |
| 当前架构兼容度 | 15% | 是否保留 Supervisor、Scheduler 和 Session 边界 | 不修改 syscall/Snapshot |
| 实现复杂度 | 10% | 新抽象和迁移面是否与当前需求相称 | 无无状态转发层 |
| 可测试性 | 10% | Local fake、contract test 和 Sandbox proof 是否可独立验证 | Backend contract 可替换 |
| 后续扩展性 | 5% | 是否能容纳 Sandbox/Remote 的路径、资源和持久化差异 | 不暴露 Host Path |

## 方案分析

### 方案一：只抽取命令执行 Backend

**描述**

保留现有 Host Workspace、Capability View、Catalog、Tool Environment 与 File
Handler。仅将 `_ShellHandler` 和 `_ToolHandler` 的 subprocess 创建迁移到
`ExecutionBackend`。

**优点**

- 首次代码改动较小。
- Supervisor 与 Prepared Execution 可以直接复用。
- 可以较快增加“在容器中执行一条普通 Shell”的演示。

**缺点**

- File Handler、`cd` 和 Catalog 仍操作 Host Workspace。
- Tool 路径、worker 路径和 venv 仍需要 Host 与 Sandbox 同时可见。
- Capability copy-up 与实际命令可能发生在不同命名空间。
- Remote Backend 仍需要为路径和文件同步添加 Handler-specific 分支。

**评估**

| 标准 | 评分 | 说明 |
|---|---:|---|
| 命名空间一致性 | 1 | 只统一进程位置，不统一文件位置。 |
| Handler 解耦 | 2 | Shell 部分解耦，Files、Tool 和 Catalog 未解耦。 |
| 生命周期正确性 | 3 | 可定义执行生命周期，但没有 Workspace 生命周期。 |
| 当前架构兼容度 | 5 | 对现有结构改动最少。 |
| 实现复杂度 | 5 | 初期工作量最低。 |
| 可测试性 | 3 | 可以 fake execute，无法证明完整 Workspace 一致性。 |
| 后续扩展性 | 1 | Remote 仍需要再次重构。 |

**工作量**：M；不包含真正可用的 Sandbox/Remote 文件语义。

**主要风险**：演示命令可以在 Sandbox 运行，但 Runtime-owned 文件操作仍修改
Host，形成难以观察的数据分叉。缓解方式最终仍是采用方案二或三。

### 方案二：Runtime-owned Backend Workspace，无 Backend Session

**描述**

Backend 打开一个同时提供 Workspace Filesystem、Bound Capability View、Tool
Runtime 与执行准备能力的 `BackendWorkspace`。一个 `AgentRuntime` 拥有一个
Backend Workspace；所有 Kernel 直接共享它，并在每次请求中传入自己的 cwd 和
环境。一次运行的 Backend job 或 process handle 由 `PreparedExecution` 持有。

**优点**

- 命令、文件、Tool 和 Catalog 共享一个明确命名空间。
- Kernel 已有 cwd/env/Execution ownership，不新增重复 Session 状态。
- Supervisor 与现有 Prepared Execution 合同保持不变。
- Local、Sandbox 和 Remote 差异集中在 Backend Workspace。
- Backend Filesystem 可以被 Catalog、`cd` 与 Runtime-owned projection 复用。

**缺点**

- Catalog 与 Library worker 需要迁移为异步 Backend Filesystem 访问。
- Backend path 不能继续使用 Host `Path`，会触及多种 facts 与测试。
- Backend Workspace 成为较宽的内部接口，需要通过子协议保持职责清晰。
- Remote Backend 若天然提供 per-client session，需要在实现内部适配。

**评估**

| 标准 | 评分 | 说明 |
|---|---:|---|
| 命名空间一致性 | 5 | 所有 Workspace I/O 都通过同一个 live Workspace。 |
| Handler 解耦 | 5 | Handler 只依赖 Backend-neutral request/filesystem contract。 |
| 生命周期正确性 | 5 | Runtime、Kernel 与 Prepared Execution 的 owner 唯一。 |
| 当前架构兼容度 | 5 | 保留 Session Kernel、Scheduler、Supervisor 与 Snapshot。 |
| 实现复杂度 | 3 | 需要迁移 Catalog、路径和 Tool Environment。 |
| 可测试性 | 5 | 可用 LocalBackend、fake filesystem 和 contract suite 分层验证。 |
| 后续扩展性 | 4 | 支持 Remote；provider-specific session 可封装在实现内。 |

**工作量**：L–XL；取决于 Capability Catalog 异步迁移的拆分粒度。

**主要风险**：一次迁移触及 Runtime-open reconcile 和 Library 后台生命周期。
缓解方式是先落地 LocalBackend contract，再按消费者逐个迁移，并在每一步保持完整
测试通过。

### 方案三：Backend Workspace 加每 Kernel 一个 Backend Session

**描述**

Backend 打开 Runtime-owned Workspace；每个 Kernel 再从 Workspace 打开一个
`BackendSession`。Shell、Tool 与 Files Handler 依赖 Backend Session，Session
持有 Backend cwd、环境或 transport state。

**优点**

- 可以直接表达 Remote provider 的 per-session token、PTY 或连接资源。
- Backend 可以主动隔离不同 Agent Session 的 transient process state。
- Kernel close 有显式 Backend resource 可以关闭。

**缺点**

- 当前 Kernel 已拥有 cwd、环境、Execution 和 close，容易产生双重状态源。
- 对 LocalBackend 而言 BackendSession 只是无状态转发层。
- “Backend Session”容易与 Agent Session、Remote sandbox session 混淆。
- 如果 BackendSession 持有独立 Workspace，既有跨 Session 文件共享语义会改变；
  如果不持有，又难以证明该抽象当前必要。

**评估**

| 标准 | 评分 | 说明 |
|---|---:|---|
| 命名空间一致性 | 5 | 在 Backend Session 仍绑定同一 Workspace 时满足。 |
| Handler 解耦 | 5 | Handler 可以只依赖 Session contract。 |
| 生命周期正确性 | 3 | 需要定义 Kernel 与 Backend Session 的重复状态边界。 |
| 当前架构兼容度 | 3 | 新增一层 Session ownership 和 close 路径。 |
| 实现复杂度 | 2 | 所有 Backend 都必须实现当前无必要的 Session 对象。 |
| 可测试性 | 4 | 可测试，但 fake 和 lifecycle case 增加。 |
| 后续扩展性 | 5 | 对天然 session-oriented provider 表达最直接。 |

**工作量**：XL；还需要 cwd、env、Workspace sharing 与 execution handle 的额外
规范。

**主要风险**：Kernel state 与 Backend Session state 漂移。缓解方式是严格规定
单一 owner，但这会使另一层退化为转发器。

### 方案比较

| 标准 | 权重 | 方案一 | 方案二 | 方案三 |
|---|---:|---:|---:|---:|
| 命名空间一致性 | 25% | 1 | 5 | 5 |
| Handler 解耦 | 20% | 2 | 5 | 5 |
| 生命周期正确性 | 15% | 3 | 5 | 3 |
| 当前架构兼容度 | 15% | 5 | 5 | 3 |
| 实现复杂度 | 10% | 5 | 3 | 2 |
| 可测试性 | 10% | 3 | 5 | 4 |
| 后续扩展性 | 5% | 1 | 4 | 5 |
| **加权结果** | **100%** | **2.70** | **4.75** | **4.00** |

## 建议

### 推荐方案

采用**方案二：Runtime-owned Backend Workspace，无 Backend Session**。

该方案同时满足命名空间一致性、Handler 解耦和既有 Execution lifecycle 约束，
并避免在当前没有独立 Session transport 或 PTY 状态时增加转发对象。未来出现以下
任一事实后，可以在不改变 Handler request 与 Prepared Execution 合同的前提下
重新评估 `BackendSession`：

- Backend 要求每个 Agent Session 持有独立认证或连接；
- 需要跨命令保留 Backend-native PTY、Shell 或进程状态；
- 一个 Kernel 必须绑定不同于 Runtime 共享 Workspace 的执行资源；
- provider API 的 cleanup 无法由 Prepared Execution 和 Backend Workspace 表达。

### 接受的取舍

1. **Catalog 异步化范围较大**：这是消除 Host Path 假设所需的直接成本，通过
   分阶段迁移和 Backend filesystem fake 控制风险。
2. **Backend Workspace 合同比单一 execute 更宽**：命令和文件必须共享命名空间，
   因此 filesystem 是必要合同；通过独立子协议避免形成无结构 God object。
3. **首期不直接获得 per-session Backend 资源**：当前 Prepared Execution 已拥有
   单次运行资源；在出现持久 Session 资源前不预先抽象。
4. **Local 与 Sandbox 的隔离强度不同**：统一的是合同和命名空间，不是安全等级。

### 采用条件

- Backend path、Filesystem result 和 Prepared Execution 不包含具体 Backend 的
  discriminator 或 payload。
- LocalBackend 不得因 Sandbox/Remote open 失败而成为隐式 fallback。
- Handler 不得通过 `isinstance(LocalBackend)` 等方式恢复 Backend 分支。
- Capability Catalog 不得要求 Remote Backend 暴露 Host-mounted mirror path。

## 技术设计

### 总体架构

```text
Host / Application
│
└── AgentRuntime
    ├── Model Provider
    ├── Capability Source / State
    ├── Backend
    │   └── BackendWorkspace                 # Runtime-owned
    │       ├── WorkspaceFilesystem
    │       ├── BoundCapabilityView
    │       ├── ToolRuntime
    │       └── PreparedExecution factory
    ├── Capability Catalogs
    │   └── read BoundCapabilityView
    └── Agent Session(s)
        ├── AgentLoop
        └── EnvironmentKernel
            ├── cwd / custom environment
            ├── Parser / Policy / Router / Scheduler
            ├── Command Handlers
            └── ExecutionSupervisor
                └── PreparedExecution(s)
```

所有 Backend Workspace consumer 使用同一个 live 实例。`AgentRuntime` 不为每个
Kernel 复制或重新 materialize Workspace。

### 所有权与生命周期

```text
AgentRuntime
    owns one BackendWorkspace
    owns Capability Catalogs and background workers

EnvironmentKernel
    borrows BackendWorkspace
    owns cwd, custom environment, Scheduler and Execution States

One admitted command
    owns one PreparedExecution
```

Runtime open 顺序：

```text
validate Host configuration
  -> open Capability Source / State
  -> Backend.open_workspace(...)
  -> bind/materialize Bound Capability View
  -> reconcile workspace MCP projections
  -> reconcile Tool Catalog
  -> reconcile Backend Tool Runtime
  -> reconcile Skill Catalog
  -> reconcile Library Catalog
  -> construct AgentRuntime
  -> start Library background worker
```

Runtime close 使用逆向依赖顺序：

```text
reject new turns
  -> close every EnvironmentKernel
     -> cancel queued/running Prepared Executions
  -> stop Library and other background workers
  -> flush Backend Workspace when configured
  -> close Backend Workspace
  -> close Capability State
```

任何 open 阶段失败都关闭已经成功打开的资源，并向调用方返回原始失败；不得重试
为权限更宽的 Backend。Tool Runtime dependency reconcile 可以保留当前 fail-soft
语义，但 `tools run` 不得回退到 Host Python。

### Backend 与 Backend Workspace

以下为用于评审职责的 preliminary contract，不固定最终模块和私有符号名称：

```python
class _Backend(Protocol):
    async def open_workspace(
        self,
        source: _WorkspaceSource,
        capability_source: _CapabilitySource,
        capability_state: _CapabilityState,
    ) -> _BackendWorkspace: ...


class _BackendWorkspace(Protocol):
    root: str
    filesystem: _WorkspaceFilesystem
    capabilities: _BoundCapabilityView
    mcp: _WorkspaceMCPRuntime

    def prepare_shell(
        self,
        request: _ShellExecutionRequest,
    ) -> _PreparedExecution: ...

    def prepare_tool(
        self,
        request: _ToolExecutionRequest,
    ) -> _PreparedExecution: ...

    async def reconcile_tool_runtime(self) -> _ToolRuntimeStatus: ...
    async def flush(self) -> None: ...
    async def close(self) -> None: ...
```

`prepare_shell` 和 `prepare_tool` 必须同步且无外部副作用。Local process spawn、
Remote job creation、network stream open 等动作推迟到返回对象的 `run()`；因此
queued execution 在实际运行前取消时不会分配 Backend 资源。

Backend-specific transport、container ID、Remote job ID 和 process group 只能存于
Backend Workspace 或 Prepared Execution 内部，不能进入 `_ExecutionState` 或
Execution Snapshot。

### Workspace Filesystem

Workspace Filesystem 是异步合同，因为 Remote 实现可能需要网络 I/O。它至少表达：

```python
class _WorkspaceFilesystem(Protocol):
    async def stat(self, path: str) -> _FileMetadata: ...
    async def list(self, path: str) -> tuple[_DirectoryEntry, ...]: ...
    async def read(self, path: str) -> bytes: ...
    async def write(self, request: _FileWriteRequest) -> _FileWriteResult: ...
    async def edit(self, request: _FileEditRequest) -> _FileEditResult: ...
    async def remove(self, path: str, *, recursive: bool = False) -> None: ...
```

结果合同使用显式 backend-neutral facts，例如 kind、size、mtime、mode、content 和
错误类别；不得返回 Host stat object、Host file descriptor 或 provider response。

`write`、`edit` 与 Runtime-owned projection write 必须定义原子提交边界。Local
Backend 可以复用临时文件加 `os.replace`；Remote Backend 可以使用 provider 的
原子 write、版本条件更新或 Backend 内 worker，但不得让 File Handler 直接在 Host
执行 read-modify-write。

Filesystem 对 managed capability path 的写入必须自动遵守 Bound Capability View
语义。File Handler 不调用 `prepare_path()`，Catalog renderer 也不执行 copy-up。

一次 Files command 仍返回 `_PreparedExecution`。建议新增通用
`_FilesystemExecution`，在 `run()` 中调用上述 filesystem operation，并在开始前
响应 cancellation。单次不可中断的远端原子写允许只提供 best-effort cancellation；
其行为不得强于当前已开始的 inline atomic replace。

### Backend path 与 cwd

Backend path 使用字符串合同，不使用 Host `Path`：

- `BackendWorkspace.root` 是模型和 Kernel 使用的实际 Workspace root；
- Local Backend 可以返回 Host absolute path；
- Sandbox/Remote Backend 可以返回 `/workspace` 等 Backend-native path；
- Kernel 只保存 Backend path cwd，不将其解析为 Host path；
- 相对路径由 Backend Filesystem 相对于 Kernel cwd 解析；
- Backend 决定其命名空间是否允许离开 Workspace root。

本 RFC 不规定所有 Backend 必须提供相同的 root confinement 强度。Sandbox 不得
把无法访问的 Host absolute path 映射成另一个静默目标；应返回明确失败。

### Command Handler

Handler 的目标职责为：

| Handler | 语义职责 | 执行方式 |
|---|---|---|
| Shell | 保留完整 raw command，生成 cwd/env request | `BackendWorkspace.prepare_shell` |
| Tools list/inspect | 读取 Runtime-open Catalog snapshot | Runtime-local text Execution |
| Tools run | 校验 Tool facts，生成 code 与 logical Tool bindings | `BackendWorkspace.prepare_tool` |
| Files write/edit | 解析 heredoc、JSON 与 edit facts | `_FilesystemExecution` |
| cd | 校验 grammar，查询 Backend directory，更新 Kernel cwd | Backend-mediated Runtime-local Execution |
| export | 校验 grammar，更新 Kernel environment | Runtime-local Execution |

`_CommandContext` 不再携带 Host `workspace: Path` 与 `cwd: Path`。它携带 Backend
root/cwd、Session environment 和允许的 Session state hook。具体字段应在实现
issue 中与 Backend request 一起收敛，参数数量继续遵守项目代码风格。

### Shell execution

Shell request 只包含 Backend-neutral 数据：

```python
@dataclass(frozen=True, slots=True)
class _ShellExecutionRequest:
    command: ShellParseResult
    cwd: str
    environment: Mapping[str, str]
```

`ShellParseResult` 是已有的纯语法事实，不包含 Host 路径或执行资源。Local Backend
可以读取其中的 output-redirection facts 完成 cooperative copy-up，其他 Backend
可以忽略不适用的 mutation hint；Backend 不重新 parse raw command。

Local Backend 在内部：

- 根据当前 Shell AST facts 完成 Local Capability copy-up preparation；
- 组装 Local execution base environment 与 Session environment；
- 创建 process group、捕获 stdout/stderr，并实现 SIGTERM 到 SIGKILL。

支持原生 overlay 的 Backend 不使用 Local mutation heuristic。Remote Backend 可以
在 `run()` 内创建 job，并通过输出 stream 或 polling 写入统一 `_ExecutionOutput`。

Backend 必须明确 execution base environment。Handler 不再读取 `os.environ`。
LocalBackend 可以显式配置为继承 Host 环境以保留当前 CLI 行为；Sandbox/Remote
不因选择 Backend 而自动获得 Provider secret。具体默认值在实现 issue 中由 Host
配置规范决定。

### Tool execution environment

Tool Catalog entry 和 Tool request 使用 logical Backend path，不保存 Host Path。
Backend Workspace 在有效 Capability View 建立后 reconcile Tool Runtime：

- Local Backend 可以继续在 Workspace state 下维护 venv；
- Sandbox Backend 可以在镜像基础上同步 Workspace requirements；
- Remote Backend 可以选择预置环境、远端 dependency cache 或 Workspace-open
  build；
- Runtime-owned Tool worker 必须随 Backend materialize，不能要求 Remote Backend
  读取 Host package resource path。

`tools run` 传递 code、cwd、Session environment 与受信任 Tool bindings。Worker
在 Backend Workspace 内加载有效 Tools，并与普通 Shell 和 Files 看到相同文件。
Tool Runtime reconcile 失败继续生成 unavailable status；`tools list/inspect` 可用，
`tools run` 明确失败且不回退 Host Python。

### Capability Source、State 与 Bound View

当前 `_CapabilityView` 中的职责拆为：

```text
Capability Source / State
    lower entries
    persistent upper facts
    whiteout / provenance
          │
          │ bind / materialize
          ▼
Backend Workspace
    Bound Capability View
    effective files
    Backend-specific overlay mechanism
```

Capability Source/State 可以在 Host 使用 `Path` 读取 Repertoire 和持久化输入，因为
它们属于 Host application state；Host Path 不能出现在 Bound View 或 Handler
合同中。

Bound Capability View 至少提供：

- 有效 capability root；
- 按受管相对路径的 provenance、shadow、whiteout 与 validation facts；
- Catalog 使用的 list/read/stat；
- Backend-specific mutation preparation，且不向 Handler 暴露。

Local Backend 可以用 file-level symlink、copy-up 和 whiteout 实现相同语义；原生
overlay、启动时物化或 Remote snapshot 不需要复用 Local 算法。

### Capability Catalog

Tool、Skill、Library 与 MCP Catalog 从 Bound Capability View 读取，不要求
Backend 提供 Host-mounted mirror。Catalog facts 中的 path 改为 Backend logical
path 或受管 relative path。

本 RFC 保持现有刷新行为：

- Tool、Skill 和 MCP projection 在 Runtime open reconcile；
- Library 在 Runtime open 建立 Catalog，并在普通模型请求前 reconcile source
  changes；
- Library background worker 通过 Backend Filesystem 读取 source 和刷新索引；
- 本次重构不增加 Tool/Skill 动态 reload。

Workspace `_mcp` 配置描述的 stdio/http MCP discovery 与 invocation 在 Backend
Workspace 侧执行，以保持 executable、网络策略和文件路径一致。未来 Host-owned
MCP connection 是不同 capability 类型，不复用 Workspace `_mcp` 配置的隐式
Host execution。

Backend Workspace 通过独立 `_WorkspaceMCPRuntime` 子协议承担 discovery；它返回
provider-neutral server/tool facts，不把 transport stream 或 provider client 暴露
给 Catalog。Invocation binding 被物化到 Backend Tool Runtime，由同一 Workspace
中的 Tool worker 使用。若本 RFC 获批，RFC-0005 中将 Workspace MCP invocation
固定在 Host Runtime IPC binding 的部分必须据此修订；bounded concurrency、可信
配置和 stub projection 等其余目标不受影响。

### Workspace persistence

持久化对象是整个 Backend Workspace 的有效变更，不只是 Capability upper：

- Local Backend 的 Host Workspace 变更立即持久；
- bind-mounted Sandbox 可以让 Host Workspace 变更立即可见；
- copy-based Sandbox 或 Remote Backend 通过显式 `flush()`/snapshot 持久化；
- Capability upper、whiteout 与普通项目文件属于同一持久化策略的不同内容。

本 RFC 只定义 lifecycle hook 和失败传播，不定义 Remote delta、merge、crash
recovery 或 checkpoint frequency。`flush()` 失败使 Runtime close 报告失败，不能
宣称变更已持久化。Backend 不得在没有显式配置时把失败的 Remote flush 改为只保留
Host 旧状态。

### 错误与诊断

Backend open 与 mandatory constraint failure 直接阻止 Runtime open。Execution
期间的 Backend 错误映射为：

- 可理解的 stderr output；
- backend-neutral `failed` 或 `killed` terminal outcome；
- 可选 Host `RuntimeDiagnostic`，detail 中不包含 secret 或 provider credential。

错误输出不得暴露 provider-specific response body、认证 header、signed URL 或包含
secret 的完整环境。

## 安全考量

| 风险 | 影响 | 可能性 | 缓解 |
|---|---|---|---|
| Backend open 失败后回退 Local | 未经同意扩大 Host 权限 | 中 | 禁止隐式 fallback，Runtime open fail closed |
| Host env 自动进入 Sandbox | Provider secret 泄露 | 高 | Backend 显式组装 execution base environment |
| Handler 绕过 Backend filesystem | Host/Backend 数据分叉或越权 | 中 | import boundary test 与 contract test |
| Remote path 被错误映射到 Host path | 读写错误目标 | 中 | Backend-native path，不接受 Host mirror 假设 |
| Capability lower 被动态 Shell 写穿 | lower 内容损坏 | Local 中、overlay 低 | Local 明示 cooperative 语义；Sandbox 使用原生只读机制 |
| Remote flush 失败被忽略 | 用户误认为变更已保存 | 中 | flush error 可见，close 不报告成功 |
| Backend diagnostic 暴露凭证 | Secret 泄露 | 中 | 结构化安全 detail，过滤环境与 transport credential |

未配置 Sandbox 时，文档继续明确 Local Backend 以 Host 用户权限运行，不提供
filesystem、network、process、Secret 或 resource containment。

## 实施计划

### 阶段一：合同与 Local Backend 骨架

- 定义 Backend、Backend Workspace、Workspace Filesystem、Backend path facts 与
  execution request。
- 让 Runtime open 创建默认 Local Backend Workspace。
- 将 `_ProcessExecution` 和 Host subprocess mechanics 移到 Local Backend。
- 增加 Backend contract fake 与 lifecycle 测试。
- 保持模型可见协议和行为不变。

### 阶段二：执行 Handler 迁移

- ShellHandler 改为生成 Shell request。
- ToolHandler 改为生成 logical Tool request，不读取 Host Python/worker Path。
- FileHandler 通过 Filesystem Execution 完成 write/edit。
- `cd` 使用 Backend Filesystem stat；`export` 保持 Runtime-local。
- 删除 Handler 对 `_CapabilityView` 和 `_ToolEnvironment` concrete type 的依赖。

### 阶段三：Capability Source/State 与 Bound View

- 从当前 `_CapabilityView` 提取 backend-neutral logical facts。
- 将 symlink、copy-up、whiteout physical mechanics 移入 Local Backend View。
- 迁移 Tool、Skill、MCP 与 Library Catalog 到 Bound View/Filesystem。
- 将 Catalog entry 的 Host Path 替换为 logical/relative path。
- 保持 RFC-0002 provenance 与 shadow 行为测试。

### 阶段四：Tool Runtime、MCP 与后台生命周期

- 让 Backend Workspace materialize worker 并 reconcile Tool dependencies。
- 让 Workspace `_mcp` discovery/invocation 使用 Backend Workspace。
- 让 Library worker 只使用 Backend Filesystem。
- 完成 Runtime open rollback、close ordering、flush failure 与 diagnostic 测试。

### 阶段五：Sandbox proof

- 实现一个最小 Sandbox Backend 或 deterministic sandbox fake。
- 证明 Shell 写入能被 Files、Tool 与 Catalog 读取，反向写入同样可见。
- 证明 Sandbox open failure 不回退 Local。
- 证明多个 Kernel 共享 Workspace 文件而不共享 cwd/env/Handle。
- 更新架构图、README 和安全声明。

### Rollback 策略

实施按 issue 拆分，每个阶段保持 LocalBackend 全量测试通过。Backend 还未成为稳定
公开 API，因此可以在同行评审前回退某个阶段的内部提交。不得通过同时保留旧
Handler Host 路径和新 Backend 路径来实现运行时 fallback；若某阶段无法通过评审，
回退该阶段提交，而不是维护双执行路径。

## 未决问题

1. **Remote persistence 的最小保证**
   - `flush()` 只在 close 调用，还是需要显式 checkpoint？
   - 断线或 Host crash 后允许丢失哪些未 flush 变更？
   - 该问题不阻塞 LocalBackend 边界重构，但在 Remote 实现前必须形成独立决定。

2. **Execution base environment 的 Host 配置面**
   - LocalBackend 是否默认继承完整 Host environment？
   - Sandbox/Remote 的显式 allowlist 或 injected environment 如何配置？
   - 无论默认值如何，Handler 不再读取 `os.environ`。

3. **Backend path 的跨平台规范**
   - Local Windows Backend 是否保留 native Windows path，还是所有 Backend 使用
     POSIX-style logical path 并由 Local adapter 映射？
   - 首个 Sandbox 实现前需要固定，以避免 Catalog facts 二次迁移。

4. **自定义 Backend public API**
   - 首期 Backend 是 private seam；如果发布 Host plugin API，需要另行确定版本、
     capability negotiation、错误稳定性与兼容策略。

5. **Remote MCP credential injection**
   - Workspace MCP 配置中的 env name/secret reference 如何安全传入 Backend，而不
     序列化 Host secret 到日志或持久 snapshot？

## 决策记录

**状态**：APPROVED。

### 2026-08-06 方向性确认

Project owner 已确认以下方向：

| 方向 | 决定 |
|---|---|
| Backend 边界 | 同时覆盖 execution 与 filesystem，不采用仅抽取 subprocess 的边界。 |
| 生命周期 | 一个 `AgentRuntime` 拥有一个共享 `BackendWorkspace`；首期不引入 `BackendSession`。 |
| Capability 分层 | Capability Source/State 与 Backend-owned Bound Capability View 分离。 |
| Workspace MCP | 采用本 RFC 当前方案，Workspace `_mcp` discovery 与 invocation 位于 Backend Workspace。 |
| 路径与环境职责 | Backend 解释 path 并组装 execution base environment；Handler 不读取 Host `Path` 或 `os.environ`。 |

这些确认锁定后续 issue 的架构方向。Remote persistence、环境默认值、跨平台
路径表示、公开 Backend API 与 Remote MCP credential injection 仍按“未决问题”
处理，并在进入对应实现阶段前形成明确决定。

**决定**：

- 采用 Runtime-owned Backend Workspace；
- Backend Workspace 同时抽象 execution 与 filesystem；
- 首期不引入 Backend Session；
- 一个 AgentRuntime 持有一个 Backend Workspace；
- Capability Source/State 与 Bound Capability View 分离；
- Backend open/constraint failure fail closed；
- 不修改模型可见 syscall 与 backend-neutral Execution lifecycle。

Project owner 于 2026-08-06 批准本 RFC。后续实现按 `docs/issues` 中对应里程碑
拆分，所有子 issue 通过同行评审后再同步 RFC 的实现状态。

## 实现状态（2026-08-07）

RFC-0012 已完整实现，`docs/issues/22-decouple-backend-workspace-and-capability-view/`
下的 11 个子 issue 全部通过同行评审并标记为 `resolved`：

| Issue | 范围 | 提交 |
|---|---|---|
| 01 | Backend Workspace 合同（execution + filesystem facts、`_PreparedExecution` 复用） | `d781e47` |
| 02 | Runtime 拥有一个 Local Backend Workspace | `1d69adb` |
| 03 | Shell 经 Local Backend 执行 | `48eedd8` |
| 04 | Files/cd 经 Workspace Filesystem | `f857d95` |
| 05 | Capability Source/State 与 Bound View 分离 | `bd9c174` |
| 06 | Tool/Skill Catalog 迁移到 Bound View | `5f3faee` |
| 07 | Library 迁移到 Workspace Filesystem | `3d558df` |
| 08 | Tool Runtime 移入 Backend Workspace | `71adee3` |
| 09 | Workspace MCP 移入 Backend Workspace | `b945e2c` |
| 10 | Backend Workspace lifecycle（open 顺序、rollback、close 顺序、fail-soft 区分） | `c8414b5` |
| 11 | 端到端证明（deterministic Sandbox proof、静态回归、架构文档） | 本 issue |

实现要点与 RFC 的一致性：

- 成功标准全部达成：Runtime 执行代码中只有 Local Backend 创建 Host subprocess；
  Handler/Catalog 不导入 Backend 具体类型、不以 Host `Path` 访问 live Workspace；
  两个 Session 共享 Workspace 文件但保持独立 cwd/env/Handle；syscall、Snapshot
  与 Supervisor 无 Backend 分支；Backend open 失败 fail closed 且不回退 Local。
- 阶段五的最小 Sandbox proof 以 deterministic in-memory Backend（`/sandbox`，
  非 Host mirror）验证：Shell 写入可被 Files、Tool 与 Tool/Skill/Library Catalog
  读取，Files/Tool 写入对后续 Shell 可见，Bound provenance 不依赖 symlink，
  两个 Kernel 共享文件不共享状态，Backend constraint failure 不回退 Local。
- RFC-0005 中拟议的 Host Runtime IPC binding placement 已按本 RFC supersede，
  实际实现为物化在 Backend Tool Runtime 内的 worker-side binding。

遗留未决问题（不阻塞 Local 边界）：Remote persistence 最小保证、Remote MCP
credential injection、跨平台 Backend path 表示、公开 Backend plugin API。

## 参考资料

- [Backend 与 Capability View 整体架构](../../discussions/backend-and-capability-view-architecture.md)
- [Session-scoped Environment Kernel](../../discussions/session-scoped-environment-kernel.md)
- [Unified Execution dispatch](../../discussions/unified-execution-dispatch.md)
- [Control plane and execution plane](../../discussions/control-plane-and-execution-plane.md)
- [OpenHands Workspace architecture](https://docs.openhands.dev/sdk/arch/workspace)
- [LangChain Deep Agents backends](https://docs.langchain.com/oss/python/deepagents/backends)
- [OpenAI Agents SDK Sandbox concepts](https://openai.github.io/openai-agents-python/sandbox/guide/)
- [Hugging Face smolagents executors](https://huggingface.co/docs/smolagents/reference/python_executors)
