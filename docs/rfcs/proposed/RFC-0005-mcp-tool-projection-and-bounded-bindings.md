---
rfc_id: RFC-0005
title: MCP Tool 投影与有界 Workspace 绑定
status: PROPOSED
author: cli-agent maintainers
reviewers:
  - name: project owner
    status: pending
created: 2026-08-01
last_updated: 2026-08-01
related_prds: []
related_rfcs:
  - RFC-0002-workspace-capability-view.md
  - RFC-0003-tool-capability-commands.md
  - RFC-0004-skill-discovery-and-loading.md
---

# RFC-0005: MCP Tool 投影与有界 Workspace 绑定

## 概述

本 RFC 实现 milestone 13「Project MCP Tools during Runtime open」与 milestone 14
「Invoke MCP Tools with bounded Workspace bindings」。用户可在 Repertoire 的
`_mcp/<server>/config.json` 描述一个 MCP Server；Runtime open 时，Repertoire
Reconciliation 并行连接各 server 一次发现其 Tool 元数据，生成真实 Python 存根
Tool 投影进 Workspace 层；模型通过既有 `tools run` 语法组合调用这些 Tool。

投影以「每次 open 全量重建」对齐描述与生成物：不留 manifest、不增量判断、不做
来源 provenance。生成物以 `mcp_<server>.py` 命名约定与手写 Tool 区分，是清理与
来源识别的唯一依据；连接失败的 server 不生成存根并发出 Runtime Diagnostic，不
保留旧产物（纯生成物语义）。

调用路径分两段落地：M13 采用存根自连（AEP 风格，每次调用新建连接），先交付
可发现、可组合的可用工具；M14 引入 Runtime 独占的 Workspace MCP Binding 与
worker 到 Runtime 的 IPC 回通道，替换存根内部实现，落地共享 client、并发预算、
`MCP_BUSY` 与 close 清理。存根表面不变，因此 A→B 是内部 swap，不返工。

本设计不新增模型可见 Syscall（仍为 `exec`、`output`、`kill`）。`_mcp/` 作为
Capability View 的托管配置目录挂载（lower/upper 合并、copy-up、whiteout），
但不出现在工具/技能 index；配置只存 env 变量名，模型不直接接触 MCP 连接描述
与凭据值。

## 背景与上下文

### 当前状态

- RFC-0002 已挂载 `tools`/`skills`/`library` 三个目录的 Capability View，提供
  file-level lower/upper 合并、copy-up、whiteout 与受信 provenance。
- RFC-0003 已实现 `tools list/info/run` 保留语法、受信 Tool Catalog、Workspace
  私有 Tool Environment 与独立 Tool lane；每次 run 在隔离 worker 进程中执行。
- RFC-0004 已实现 Skill Catalog 与紧凑模型上下文广告，确立了
  「Catalog 派生 + 生成 index + 按需加载」的重复模式。
- Runtime open 链路（`runtime.py::_reconcile`）目前为：
  `_prepare_workspace` → `_load_workspace_env` → `_CapabilityView.open` →
  `_ToolCatalog.reconcile` → `_ToolEnvironment.reconcile` → `_SkillCatalog.reconcile`。
- 运行时依赖含 httpx、jsonschema、python-dotenv、strictyaml；尚无官方 `mcp` 包。
- `ToolEntry.provenance` 只区分 `repertoire` / `workspace`，MCP 存根按普通
  Workspace Tool 呈现，不做额外 provenance 标记。
- 现有「diagnostic」仅为 CLI 表现层对 model event 的 stderr 渲染，不存在
  Runtime → Host 的结构化诊断通道。

### 术语表

| 术语 | 定义 |
|---|---|
| MCP 描述 | `_mcp/<server>/config.json`：用户对某 MCP Server 的传输与启动描述；`_mcp` 是 Capability View 的托管配置目录，支持 Repertoire 挂载与 Workspace 覆盖/白名单；不出现在工具/技能 index。 |
| MCP 描述 | `_mcp/<server>/config.json`：用户对某 MCP Server 的传输与启动描述；`_mcp` 是 Capability View 的托管配置目录，支持 Repertoire 挂载与 Workspace 覆盖/白名单；不出现在工具/技能 index。 |
| MCP 投影 | Runtime open 时从描述发现 Tool 元数据后生成的真实 Python 存根文件 `.workspace/tools/mcp_<server>.py`，按普通 Workspace Tool 呈现。 |
| MCP 命名约定 | 生成存根一律以 `mcp_<server>.py` 命名；用户约定不使用 `mcp_` 前缀书写手写 Tool。它是清理（删除 `tools/mcp_*.py`）与来源识别的唯一依据。 |
| Workspace MCP Binding | Runtime 独占的 live MCP client 集合：每 server 一个共享 `ClientSession`、并发预算、自启进程句柄；同 Runtime 的 Session 共享，不同 Workspace 永不共享。 |
| MCP Concurrency Budget | 每 server 有界的 in-flight 请求数 + 排队等待数；满队立即返回 `MCP_BUSY`。 |
| MCP IPC 回通道 | worker 进程与 Runtime 主进程之间的通道：存根 `_call_mcp` 把工具调用写成请求经通道交给 binding，读回结构化结果。 |
| Minimal MCP Client | Runtime 只使用 `list_tools` 与 `call_tool`，不暴露 sampling、elicitation、roots 等 server 反溯能力。 |
| Runtime Diagnostic | Runtime 向 Host 发出的结构化通知，host 记录或渲染；MCP 发现重试耗尽时使用。 |

### 历史上下文

AEP 的实际实现（`Agent-Environment-Protocol/src/aep/capability/tool/mcp/`）用
`_mcp/<server>/config.json` 存配置（含**字面量** env/headers），`add` 时连接一次
发现工具，`stubgen` 生成 `tools/<name>.py` 存根，每次函数调用 `asyncio.run` 新建
连接后 `call_tool`。其局限：无 live client、无并发预算、凭据字面量落盘、生成物
无受信 provenance、无诊断与失效语义。

cli-agent 遵循 AEP-native 架构决定（`CONTEXT.md`、scratch 决策 05/09/10），在
AEP 可用的命令表面之上补齐：Secret 引用化（本 RFC 采用 `.workspace/env` 注入
的降级形态）、Workspace 级连接独占、并发预算、结构化诊断。来源区分以 `mcp_`
命名约定取代 AEP 的 provenance 派生，清理与识别都依赖该约定（见「与架构的
偏差」）。「注册」对应 Runtime-open 的 Reconciliation 而非 AEP 的
`add_mcp_server` CLI。

## 问题陈述

### 问题

模型目前无法使用任何 MCP 能力。若照搬 AEP：

- 凭据字面量写入配置与生成存根，违背决策 10 的 Secret Reference 契约；
- 每次调用新建连接，无并发预算，server 慢会无界阻塞 worker；
- 连接与进程由 worker 临时持有，Runtime 无法在 close 时统一清理；
- 生成物与手写 Tool 无法区分来源，删除描述可能误删用户文件。

### 不作为的影响

- MCP 生态能力不可达，模型只能通过 Host 预置的本地 Tool 工作。
- 后续任何 MCP 集成都需重新设计配置面、执行路径与安全边界。

## 目标与非目标

### 目标

1. 用户在 Repertoire 或 Workspace 的 `.workspace/_mcp/<server>/config.json` 写
   配置即可请求一个 MCP Server 的能力；Workspace 可覆盖或白名单 Repertoire
   描述。
2. Runtime open 并行调和描述 → 生成真实 Python 存根 Tool；发现失败重试 ≤3 次，
   耗尽发 Runtime Diagnostic 且不阻塞 open；失败服务器本次不生成存根，不留旧
   产物（纯生成物语义）。
3. 模型通过既有 `tools run` 组合调用 MCP Tool，可在一个代码段内混用本地 Tool
   与 MCP Tool。
4. 配置与存根只存 env 变量名，值在运行时从 `os.environ | session_env`（含
   `.workspace/env`）解析，不写入生成文件。
5. M14 落地 Workspace MCP Binding：Session 间共享多路复用 client、每 server
   并发预算、满队 `MCP_BUSY`、Runtime close 清理连接与自启进程。
6. 删除 server 描述/改名/发现失败时，只删除 `mcp_` 前缀命名约定的生成物，永不
   删除手写文件。

### 非目标

1. 提供 `mcp add/remove` 类 Agent-可见命令（配置面由用户直接维护 Repertoire
   与 Workspace 配置目录）。
2. 把 MCP 结果视为安全或良性；断连/失败按普通失败 Tool Result 处理。
3. 热重载：运行中的 Runtime 不感知 MCP 描述变化，下次 open 调和。
4. 引入 sampling、elicitation、roots 等 server 反溯能力。
5. 在 M13 落地并发预算与共享 client（属于 M14）。
6. 承诺 Secret 保密边界：`.workspace/env` 值是模型可读数据，env 名注入是便捷
   引用而非保密，见「与架构的偏差」。
7. 增量重建与保留生成物：每次 open 全量重建存根，不增量判断，不保留旧产物
   （用户对生成存根的修改会在下次 open 被重建覆盖）。
8. 为 MCP 生成物提供独立的 provenance 标记：来源以 `mcp_` 命名约定与模块
   docstring 识别，不引入 ToolEntry 新值。

## 评估标准

| 标准 | 权重 | 描述 | 最低阈值 |
|---|---:|---|---|
| 表面稳定 | 高 | 不新增 Syscall / 命令头 / schema | syscall 仍为三个 |
| 来源可辨 | 中 | 生成物以 `mcp_` 前缀命名，清理/识别不误伤手写文件 | 删除描述只删 `mcp_*.py` |
| 组合能力 | 高 | 模型可在 `tools run` 中混合本地与 MCP Tool | 混合代码段可执行 |
| Workspace 隔离 | 高 | 不同 Workspace 不共享连接/凭据/进程 | 每 Workspace 独立 binding |
| 有界并发 | 高 | in-flight + 排队有界，满队 `MCP_BUSY` | M14 交付 |
| 生命周期 | 高 | Runtime close 清理连接与自启进程 | M14 交付 |
| 令牌经济 | 中 | 系统消息只广告紧凑目录 | 不嵌入完整工具元数据 |

## 方案分析

### 方案 1：直接移植 AEP（stub 自连 + 字面量配置）

**描述**

照搬 AEP：`_mcp/<server>/config.json` 存字面量 env/headers，`add` 时发现，
`stubgen` 生成自连存根，每次调用新建连接。

**优点**

- 实现最简，M13 即可交付可用工具。
- 与 AEP 行为差异最小，示例可直接复用。

**缺点**

- 凭据字面量落盘，违背 Secret Reference 契约。
- 无并发预算、无共享 client、无 close 清理，完全放弃 M14 契约。
- 生成物无受信 provenance，删除描述无归属依据。

**评估**

| 标准 | 评分 | 说明 |
|---|---|---|
| 表面稳定 | 好 | 不新增 syscall |
| 来源可辨 | 差 | 无命名约定 |
| 组合能力 | 好 | 存根函数真实可组合 |
| Workspace 隔离 | 可 | 每 Workspace 各起进程 |
| 有界并发 | 差 | 无预算 |
| 生命周期 | 差 | 连接不受 Runtime 掌控 |

**工作量**: S。**风险**: 安全契约与并发契约全部缺失。

### 方案 2：全量投影 + 有界绑定（本 RFC）

**描述**

配置面只存描述与 env 变量名；M13 并行发现并生成真实 Python 存根（以
`mcp_<server>.py` 命名、每次 open 全量重建、不留 manifest），M14 以 Runtime
独占的 Workspace MCP Binding + IPC 回通道替换存根内部实现，落地共享、预算、
清理。

**优点**

- 生成物是真实文件，`tools list/info` 复用现有 Catalog/index 骨架，几乎零改动。
- 存根表面在 A→B 中不变，组合调用契约稳定。
- 无 manifest、无增量判断：清理 = 删除 `mcp_*.py` 后重建，逻辑最简。
- M13 与 M14 解耦：投影/诊断先行，binding/IPC 后置，可逐步评审。
- env 名注入避免凭据落盘。

**缺点**

- 每次 open 无条件重连所有服务器（并行缓解，最慢者决定启动时长）。
- 需要新增 `mcp` 运行时依赖与 Tool Environment 基础依赖注入。
- M14 需要新增 IPC 传输层、预算层与相关测试。
- 来源区分依赖 `mcp_` 命名约定（非受信 provenance，见「与架构的偏差」）。
- `.workspace/env` 注入是模型可读数据，放弃保密边界（记录为偏差）。

**评估**

| 标准 | 评分 | 说明 |
|---|---|---|
| 表面稳定 | 好 | 不新增 syscall |
| 来源可辨 | 可 | `mcp_` 前缀约定派生 |
| 组合能力 | 好 | 真实函数 + 允许混合 |
| Workspace 隔离 | 好 | 每 Workspace 独立 binding |
| 有界并发 | 好 | M14 落地 |
| 生命周期 | 好 | M14 落地 |

**工作量**: M（M13）+ L（M14）。**风险**: IPC 层复杂度与取消/失败语义；来源
区分依赖用户不使用 `mcp_` 前缀的约定。

### 方案 3：纯 Catalog 条目（不落盘投影）

**描述**

发现结果只进内存 `_MCPCatalog`，与 `_ToolCatalog` 合并，不生成文件。

**优点**

- 无生成文件，无"模型改坏存根"的边界。
- 实现面更干净。

**缺点**

- `tools list/info` 需改 Catalog 为多来源合并。
- 无文件形态投影，偏离决策 05「generated Python Tools」契约。
- 模型无法 `cat` 查看生成工具内容。

**评估**

| 标准 | 评分 | 说明 |
|---|---|---|
| 表面稳定 | 可 | 需改 Catalog 来源 |
| 来源可辨 | 可 | 命名约定派生 |
| 组合能力 | 可 | 依赖语法特判 |
| Workspace 隔离 | 好 | 无文件 |
| 有界并发 | 好 | 同方案 2 |
| 生命周期 | 好 | 同方案 2 |

**工作量**: M。**风险**: 偏离架构契约，Catalog 双来源复杂度。

### 方案对比汇总

| 标准 | 直接移植 | 全量投影 + 有界绑定 | 纯 Catalog 条目 |
|---|---|---|---|
| 表面稳定 | 好 | 好 | 可 |
| 来源可辨 | 差 | 可 | 可 |
| 组合能力 | 好 | 好 | 可 |
| Workspace 隔离 | 可 | 好 | 好 |
| 有界并发 | 差 | 好 | 好 |
| 生命周期 | 差 | 好 | 好 |

## 建议

采用方案 2，分 M13/M14 两段落地。接受的取舍：

1. 投影载体：真实 Python 存根文件 `mcp_<server>.py`，复用现有 Tool
   Catalog/index/Driver 骨架。
2. 调用形态：`tools run` 单表达式与组合代码段，允许本地/MCP 混合引用。
3. Secret 形态：复用 `.workspace/env` 注入，存根/配置只存 env 变量名。
4. 执行路径：M13 并行发现、每次 open 全量重建（清理 `mcp_*.py` 后重新生成、
   失败即无），存根自连（AEP 风格）；M14 以 Workspace MCP Binding + IPC
   回通道替换内部实现，存根表面不变。
5. 依赖：新增官方 `mcp` 运行时依赖，并在 Tool Environment 注入 Runtime 基础
   依赖。
6. 诊断：新增最小化 Runtime Diagnostic seam，任一 server 发现重试耗尽时使用。
7. 来源区分：`mcp_` 命名约定，无 manifest、无增量判断、无 provenance 标记。

### 与架构的偏差

- **Secret Reference 契约降级**：架构要求配置只存引用、host 运行时供值、值从
  所有受管文件与诊断中排除。本 RFC 采用 `.workspace/env` 环境变量名引用——
  `.workspace/env` 值是模型可读数据（决策 17 已接受完整环境继承），因此这是
  **便捷引用而非保密边界**。本项目当前把保密边界交给 Host 外部沙箱，而非
  Runtime 内部承诺。
- **受信 provenance 降级为命名约定**：架构决策 05 要求生成 Tool 有受信来源。
  本 RFC 以 `mcp_` 文件名前缀约定取代 manifest 派生：来源识别与清理依赖
  「用户手写 Tool 不以 `mcp_` 开头」的约定，用户违反约定可能被误删/误判。这是
  为换取「无 manifest、全量重建」的简化而接受的已知偏差。

## 技术设计

### 架构

```text
Repertoire/_mcp/<server>/config.json ── 精确链接挂载 ──▶ .workspace/_mcp/<server>/config.json
（用户拥有，托管配置目录）                     │          （可被真实文件覆盖或 whiteout 禁用）
        │
Runtime open ─ Repertoire Reconciliation
        │       1. 读 .workspace/_mcp 视图中的 config.json（jsonschema 校验，失败 → 诊断，跳过）
        │       2. 并行 mcp SDK list_tools() 发现（每 server 重试 ≤3 次，耗尽 → 诊断）
        │       3. 删除 .workspace/tools/mcp_*.py（清理旧产物）
        │       4. 为每个成功发现的 server 生成 .workspace/tools/mcp_<server>.py
        ▼
   _ToolCatalog.reconcile  ──→  tools/index.md（普通 Workspace Tool 呈现）
        │
模型 tools run "..." ──→ Tool Driver
        │                    ├─ 纯本地/混合：既有 worker（M13 存根自连；M14 注入 IPC shim）
        │                    └─ M14：binding 持唯一 ClientSession，预算 + MCP_BUSY
        ▼
   Workspace MCP Binding（每 Workspace 独占；Runtime close 清理）
```

### 配置面 `_mcp/<server>/config.json`

`_mcp` 加入 `_CAPABILITY_DIRECTORIES`，作为托管配置目录挂载：Repertoire 描述以
精确 lower 链接出现在 `.workspace/_mcp/`，用户可写真实文件覆盖，或以 whiteout
禁用；新增 server 也可只在 Workspace 层书写。它不出现在工具/技能 index，配置只
存 env 变量名，因此挂载不泄露凭据。`<server>` 目录名规则与 Skill 名一致：小写字母、
数字、连字符，≤64，且必须等于 `name`。

```json
{
  "name": "github",
  "transport": "stdio",
  "command": ["npx", "-y", "@modelcontextprotocol/server-github"],
  "env": ["GITHUB_TOKEN"]
}
```

| 字段 | 必需 | 规则 |
|---|---|---|
| `name` | 是 | 非空，小写字母/数字/连字符，≤64，等于目录名 |
| `transport` | 是 | 枚举 `stdio` / `http` |
| `command` | stdio | 非空字符串数组，server 启动命令与参数 |
| `url` | http | 非空字符串 |
| `env` | 否 | **env 变量名字符串数组**；运行时从 `os.environ \| session_env` 解析，不存值 |
| `headers` | 否 | 映射 header 名 → env 变量名；运行时解析 |

配置经 jsonschema 严格校验；结构非法 → 该 server 记为错误、进诊断、不生成
投影，不阻塞 open。

### 生成物命名约定与清理

投影不使用 manifest，来源识别与清理只依赖命名约定：

- 存根一律命名为 `.workspace/tools/mcp_<server>.py`；用户约定不使用 `mcp_`
  前缀书写手写 Tool。
- 每次 open 的对齐分两步：先删除 `.workspace/tools/mcp_*.py`，再为成功发现的
  server 生成新存根。这样「描述被删除」「server 改名」「发现失败」三种情况的旧
  产物都会自动消失。
- 清理永不触碰非 `mcp_` 前缀的文件；生成存根只写 `mcp_<server>.py` 目标路径。
- MCP 存根在 Tool Catalog 中按普通 Workspace Tool 呈现，不引入 `mcp`
  provenance 值；来源信息由模块 docstring（server 名、transport）与文件名前缀
  表达。
- 用户覆盖或删除存根文件：覆盖后本次会话按实际内容生效；下次 open 全量重建时
  会被重新生成覆盖；删除后下次 open 重新生成。纯生成物语义，不承诺保留用户
  修改。

### 存根生成（M13 与 M14 共用表面）

`.workspace/tools/mcp_<server>.py` 为真实 Python 文件，模块 docstring 含 server
名、传输与工具列表；每个发现的 Tool 生成一个带类型注解与 docstring 的函数；
另提供通用 `call(tool_name, **kwargs)`。

M13 内部实现（自连）：

```python
_ENV_NAMES = ["GITHUB_TOKEN"]

def create_issue(title: str, body: str = None):
    """..."""
    return _call_mcp("create_issue", {"title": title, "body": body})

def _call_mcp(tool_name, arguments):
    env = {name: os.environ[name] for name in _ENV_NAMES}
    # asyncio.run → stdio/streamable_http 连接 → ClientSession.initialize()
    # → call_tool → 提取 content 的 text/data → 返回字符串
```

M14 内部实现（IPC 回通道）：

```python
def _call_mcp(tool_name, arguments):
    return mcp_shim.call("github", tool_name, arguments)  # 走 worker 注入的 shim
```

存根只存 env **名**，值运行时解析；生成文件不含任何字面量凭据。

### 调用路径

- 语法沿用 `tools run "..."` 与 `tools run PY<< ... PY`；`_tool_facts` 静态解析
  引用，`tools.mcp_<server>.<tool>` 归入 `tools.mcp_<server>` 模块名（存根文件
  stem）；存根内部 `_call_mcp` 以配置名 `<server>` 标识路由。
- **允许混合引用**：一个 `tools run` 代码段可同时引用本地 Tool 模块与 MCP
  存根。worker 是统一 Python 执行环境，本地模块 load 进命名空间、MCP 调用经
  shim/通道，混合自然成立。
- 并行安全沿用既有分析：静态引用集合非空、无动态访问、全部在 Host
  `parallel_tools` allowlist 才可并行；MCP 引用额外受 binding 并发预算约束。
- MCP 调用失败/断连 → 普通失败 Tool Result；**不删除生成 Tool**。

### Workspace MCP Binding（M14）

- Runtime open 构建 `_WorkspaceMCPBinding`，每 server 持一个共享
  `ClientSession`（stdio 启动子进程 / http 长连接）。
- Session 间共享；不同 Workspace 永不共享连接、凭据、进程或可变 server 状态。
- 每 server 一个 `MCP Concurrency Budget`：`in_flight` 上限 + 有界排队；满队
  立即返回 `MCP_BUSY`（映射为失败 Tool Result）。建议默认 `in_flight=4`、
  `queue=32`，Host 可配置（留待 conformance 确认）。
- Minimal Client：只使用 `list_tools` 与 `call_tool`。
- Runtime close：关闭所有连接并终止自启的 MCP server 进程。

### IPC 回通道（M14）

- Tool Driver 在 spawn worker 时用 `socketpair`（或 pipe 对）建立通道，经
  `pass_fds` 传给 worker，另一端由 Driver 侧 task 服务。
- worker 注入只读 shim（如 `cli_agent_mcp`），存根 `_call_mcp` 写
  `{"server": ..., "tool": ..., "args": ...}` 请求并读回
  `{"ok": true, "result": ...}` 或 `{"ok": false, "code": ..., "error": ...}`。
- 请求进 binding 前先过预算；`kill` 传播取消到 binding 与 MCP 调用；execution
  结束或 worker 退出关闭通道。

### Tool Environment 基础依赖

- `_ToolEnvironment.reconcile` 的有效 requirements = 用户
  `.workspace/tools/requirements.txt` + Runtime 注入的基础依赖（`mcp`），保证
  M13 存根在 worker venv 可导入 `mcp`。M14 后存根不再需要 `mcp`，注入可移除。

### Runtime Diagnostic

- 新增 `RuntimeDiagnostic` 结构化数据（kind、message、detail 字段）。
- `AgentRuntime.open(..., on_diagnostic: Callable[[RuntimeDiagnostic], None] | None
  = None)`；参考 CLI 接入 stderr。
- 触发点：任一 server 的 MCP 发现重试 3 次耗尽，或 config 缺失/结构非法
  （不阻塞 open、该 server 本次不生成存根）。诊断信息不含任何 env 值或凭据。

### 模型上下文

- `tools list` / `tools info` 将生成的 `mcp_<server>` Tool 作为普通 Workspace
  Tool 列出，无需新命令头、无 provenance 扩展。
- 模型通过 stub 模块 docstring（server 名、transport、工具列表）与 `mcp_`
  文件名前缀识别来源，用 `tools run` 调用。

## 安全考量

| 威胁 | 影响 | 可能性 | 缓解 |
|---|---|---|---|
| MCP server 返回恶意/注入内容 | 高 | 中 | 结果为模型输入，无安全声明；受既有 Policy/审批与 Host 沙箱控制 |
| 凭据经存根落盘 | 高 | 低 | 只存 env 变量名，运行时解析 |
| 模型覆盖生成存根 | 中 | 中 | 覆盖后按实际内容生效并可见；下次 open 全量重建覆盖（纯生成物语义） |
| 并发无界导致 server 过载 | 高 | 中 | M14 并发预算与 `MCP_BUSY` |
| 删除描述误删手写文件 | 高 | 中 | 清理只删 `mcp_` 前缀文件；用户违反约定可能误删（已知偏差） |
| worker 通道被滥用 | 中 | 低 | 通道只读、每 execution 一通道、只转发到对应 binding |

结构校验与命名约定不构成安全认证；外部 server 内容不信任。

## 实施计划

### Milestone 13：投影 MCP Tools（先自连）

1. `pyproject.toml` 加入 `mcp`，刷新 lock；`_prepare_repertoire` 增加 `_mcp`。
2. `_capability/mcp/`：config schema + jsonschema 校验 + `MCPServerConfig` 事实。
3. `_MCPCatalog.reconcile`：并行发现（每 server 重试 ≤3）、清理
   `tools/mcp_*.py`、生成 `tools/mcp_<server>.py` 存根、诊断触发；删除
   manifest/digest/增量判断逻辑。
4. 不引入 Tool Catalog provenance 扩展：MCP 存根按普通 Workspace Tool 呈现。
5. `_ToolEnvironment` 注入基础依赖（`mcp`）；存根自连实现。
6. Runtime Diagnostic seam：`on_diagnostic` + 参考 CLI stderr 渲染。
7. `tools run` 混合引用支持（自连形态下天然成立，仅补测试）。
8. 新增 `tests/test_mcp_projection.py` 等；更新 `docs/handoff.md` 与 milestone
   issue 记录；断言 syscall surface 不变。

### Milestone 14：有界 Workspace 绑定

1. `_WorkspaceMCPBinding`：每 server 共享 `ClientSession`、子进程句柄、预算、
   生命周期；close 清理。
2. IPC 回通道：socketpair、帧协议、Driver 集成、worker shim、请求/响应关联、
   超时与取消传播。
3. 预算与 `MCP_BUSY`：满队立即失败，进公共 Policy/调度 gate。
4. 失败语义：断连 → 普通失败 Tool Result；不删除生成 Tool。
5. 移除 M13 自连依赖（存根改走 shim；venv 基础依赖可清理）。
6. 新增 `tests/test_mcp_binding.py` 等；更新 `docs/handoff.md`。

### 回滚策略

移除 `_MCPCatalog.reconcile` 接入即可恢复原行为，不影响手写 Tool 与本地 Tool
执行。生成的 `.workspace/tools/mcp_*.py` 与 `_mcp/` 相关模块可识别为 Runtime
拥有状态；回滚不得删除 Repertoire 或 Workspace 的手写能力文件。

## 未决问题

- MCP Concurrency Budget 默认值（`in_flight=4`、`queue=32` 的提议待确认）。
- HTTP transport 的 header 引用与重定向/代理处理是否需要 v1 覆盖。
- `mcp_` 前缀约定是否需要强制保护（如对 `mcp_*.py` 的写路径报错），还是先按
  「清理只删 `mcp_*.py`」的已知偏差处理。
- `mcp` 官方包在 M13 自连模式下必须存在于 worker venv，与决策 09「统一安装」的
  关系需在依赖评审中确认。

## 决策记录

**状态**: PROPOSED

**日期**: 2026-08-01

**审批人**: 待定（project owner）

### 决策摘要

采用全量投影 + 有界绑定：`_mcp/<server>/config.json` 为配置面，Runtime open
并行发现、清理 `mcp_*.py` 后生成真实 Python 存根 Tool（`mcp_<server>.py`），
`tools run` 组合调用且允许本地/MCP 混合；无 manifest、无增量判断、无
provenance 标记，来源区分依赖 `mcp_` 前缀约定；M13 先以存根自连交付可用工具，
M14 以 Workspace MCP Binding + IPC 回通道替换内部实现落地共享、预算与清理。

### 关键讨论点

1. 投影载体：真实 Python 存根文件（复用既有 Tool 骨架），而非纯 Catalog 条目。
2. 调用形态：`tools run` 组合，允许 worker 内混合本地/MCP 引用。
3. Secret 形态：复用 `.workspace/env` 注入，只存 env 变量名（对 Secret
   Reference 契约的已记录偏差）。
4. 来源区分：`mcp_` 文件名前缀命名约定，取代 manifest 与 provenance 派生
   （对受信 provenance 的已记录偏差）；清理 = 删 `mcp_*.py` 后全量重建。
5. 执行路径：A（自连）→ B（IPC binding）两段式，存根表面不变。
6. 诊断：新增最小化 Runtime Diagnostic seam；发现并行，任一 server 耗尽独立
   上报。

### 批准条件

- 代码须经同行评审后方可提交。
- `_mcp` 挂载进 Capability View（托管配置目录，支持 Repertoire 挂载与 Workspace
  覆盖/白名单）；模型可见 surface 仍为 `exec`/`output`/`kill`，`_mcp` 不出现在
  工具/技能 index。
- `mcp_` 前缀约定是清理与来源识别的唯一依据；清理只删 `mcp_*.py`，永不触碰
  其他文件。

### 反对意见

暂无记录。

## 参考资料

- `docs/rfcs/approved/RFC-0002-workspace-capability-view.md`
- `docs/rfcs/approved/RFC-0003-tool-capability-commands.md`
- `docs/rfcs/proposed/RFC-0004-skill-discovery-and-loading.md`
- `../.scratch/cli-agent-runtime/issues/13-project-mcp-tools-during-runtime-open.md`
- `../.scratch/cli-agent-runtime/issues/14-invoke-mcp-tools-with-bounded-bindings.md`
- `../.scratch/aep-native-agent-runtime/issues/05-define-repertoire-content-conventions.md`
- `../.scratch/aep-native-agent-runtime/issues/10-define-trust-and-security-invariants.md`
- `../.scratch/aep-native-agent-runtime/issues/15-decide-stale-mcp-tool-failure-semantics.md`（本 RFC 以「失败即无、不留旧产物」作答）
- `/Users/huangzhenghao/Code/Python/Agent-Environment-Protocol/src/aep/capability/tool/mcp/`
- `../CONTEXT.md`
