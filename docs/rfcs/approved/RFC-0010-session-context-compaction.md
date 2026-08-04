---
rfc_id: RFC-0010
title: Session Context Management and Four-Tier Compaction
status: COMPLETED
author: cli-agent maintainers
reviewers:
  - name: project owner
    status: approved
created: 2026-08-04
last_updated: 2026-08-04
decision_date: 2026-08-04
related_prds: []
related_rfcs:
  - RFC-0003-tool-capability-commands.md
  - RFC-0006-explicit-runtime-resource-ownership.md
  - RFC-0007-unified-command-routing-and-execution-refactor.md
---

# RFC-0010: Session Context Management and Four-Tier Compaction

## 概述

本 RFC 提议为每个 Agent Session 引入一个 `_ContextManager`，由它统一拥有模型
Conversation History、上下文预算、Token 用量观测和压缩状态。`AgentLoop` 在每次
向 Model Provider 发出普通请求前调用 `prepare_request()`；每次收到完整
`ModelCompletion` 后调用 `observe()`，再将 Assistant Message 和随后的 Tool Result
追加到 Context Manager。一个用户 Turn 内可能包含多个 Model Step，因此压缩边界是
“每次普通 Model Request 之前”，而不是只在用户 Turn 结束时执行。

Context Manager 使用四级水位线管理下一次请求的输入预算：低于 60% 不处理；达到
60% 时截短保护区外的旧 Tool Result；达到 80% 时将已经截短的旧 Tool Result
进一步替换为结构化占位内容；达到 95% 且前两级仍无法释放足够空间时，才调用一次
无 Tools 的模型摘要请求，将旧摘要与保护区之前的完整历史 Turn 合并成新摘要。
Tier 1 和 Tier 2 第一版只处理 Tool Result；User Message 和普通 Assistant 文本只会
在 Tier 3 中随完整历史 Turn 进入摘要。

该设计保留 Provider-neutral 的 Runtime 语义，不依赖特定供应商的服务端压缩接口。
Provider 原生 compaction 可以在未来作为 Adapter 优化加入，但不得改变 Runtime
定义的保护区、Tool Call 配对、摘要边界或失败语义。

## 目录

- [背景与上下文](#背景与上下文)
- [问题陈述](#问题陈述)
- [目标与非目标](#目标与非目标)
- [设计原则](#设计原则)
- [评估标准](#评估标准)
- [选项分析](#选项分析)
- [建议方案](#建议方案)
- [技术设计](#技术设计)
- [安全与隐私](#安全与隐私)
- [实施计划](#实施计划)
- [测试策略](#测试策略)
- [发布、回滚与迁移](#发布回滚与迁移)
- [风险与缓解](#风险与缓解)
- [未决问题](#未决问题)
- [决策记录](#决策记录)
- [参考资料](#参考资料)

## 背景与上下文

### 当前实现

当前 Session 的 Conversation History 由 `runtime/_agent_loop.py:AgentLoop` 直接持有：

```python
self._history: list[ModelMessage] = [system_message]
```

每次运行会依次追加 User Message、Assistant Message 和 Tool Result Message，并在每个
Model Step 中把完整 `_history` 放入 `ModelRequest`。历史只增不减，也没有水位、保护区、
压缩状态或 Context Overflow 恢复逻辑。

现有 Provider-neutral model contract 已包含：

- `SystemMessage`、`UserMessage`、`AssistantMessage` 和 `ToolResultMessage`；
- 保留 Assistant 内容顺序和 `call_id` 的 `ToolCall`；
- 通过 `call_id` 与 Tool Call 对应的 `ToolResult`；
- 带 `input_tokens`、`output_tokens` 和 `total_tokens` 的 `ModelUsage`；
- `ModelCompletion.usage`，Provider 不报告用量时为 `None`。

`OpenAICompatibleModelProvider` 已请求流式 usage，并将 `prompt_tokens` 映射为
`ModelUsage.input_tokens`。但是 Agent Loop 只把 completion 向 Host 转发，没有将
usage 与生成它的请求版本关联，也没有用它管理下一次请求。

当前 `ModelRequest.tools` 固定为三个 Runtime syscall 且不可由调用方覆盖。Tier 3
摘要必须使用无 Tools 的独立请求，以免摘要模型产生 Tool Call 或摘要请求进入正常
Environment dispatch，因此该契约需要允许显式传入空 Tools。

### Tool Result 是首要压缩对象

当前 Environment 对每个 Execution 最多保留 2,000 个输出块或 1 MiB 数据；
`exec`、`output` 和 `kill` 一次默认可向模型返回 200 个输出块。一个 Session 中多次
读取文件、执行搜索或轮询进程后，重复且可重新获取的 Execution snapshot 会成为
History 的主要增长来源。

Execution State 在 Session 关闭前持续保留，旧 snapshot 中的 `exec_id`、Cursor、
状态和退出码也具有稳定语义。因此 Context Manager 可以优先移除旧 snapshot 的大段
`chunks`，同时保留识别信息和重读提示。错误 Tool Result 通常较短且影响后续决策，
第一版不对其做 Tier 1/2 压缩。

### 外部调研

长上下文没有超过模型标称窗口也不意味着其中的信息可被同等利用。
“Lost in the Middle” 的实验表明，相关信息位于长上下文中部时，模型表现可能明显
低于信息位于开头或末尾时。四级水位线因此不仅用于避免硬性超限，也用于减少旧的、
低价值内容对注意力的稀释。

Anthropic Context Editing 会从最旧的 Tool Result 开始清理，保留最近的 Tool Use，
使用占位内容，并提供 `exclude_tools` 与 `clear_at_least`。后者用于确保一次清理释放
足够多的 Token，使 Prompt Cache prefix 失效的代价值得承担。这些行为支持本 RFC
采用“旧结果优先、保护最近内容、最小回收量”的规则。

LangChain 将临时 trim/delete 与持久 summary 分开，并在模型调用前处理短期记忆。
公开的并行 Tool Call 摘要边界问题也说明：如果截断位置落在并行 Tool Result 中间，
可能留下缺少对应 Assistant Tool Call 的非法请求。Context Manager 因此必须按完整
Tool Exchange 和完整 Turn 计算边界，不能按任意 Message 数量或 Token 位置切片。

OpenAI Responses 和 Anthropic Messages 已提供服务端 compaction。它们能降低应用
侧实现成本，但返回类型、状态管理和可用 endpoint 均与供应商绑定；当前 cli-agent
只有通用 OpenAI-compatible Chat Completions Adapter，无法把供应商能力作为统一
Runtime contract。

### 术语

| 术语 | 定义 |
|---|---|
| Model Step | 一次普通 Model Request 及其完整 Model Response。 |
| User Turn | 从一个 User Message 开始，经过零个或多个 Tool Exchange，到一个不再请求 Tool 的 Assistant Message 结束。 |
| Tool Exchange | 一个含一个或多个 Tool Call 的 Assistant Message，以及包含全部对应 `call_id` 的 Tool Result Message。 |
| Active Turn | 尚未由不含 Tool Call 的 Assistant Message 正常结束的 User Turn。 |
| Context Revision | Context Manager 每次追加或替换内容后递增的逻辑版本。 |
| Reported Usage | Provider 对一个已发送请求返回的真实 `input_tokens`。 |
| Estimated Usage | 对 Reported Revision 之后新增或替换内容所做的保守 Token 估算。 |
| Input Budget | Context Window 扣除输出预留和安全余量后，普通请求允许使用的输入 Token。 |
| Protected Suffix | History 末尾不参与普通水位压缩的完整 Turn 集合。 |
| Summary Frontier | 已被当前摘要覆盖的最后一个完整历史 Turn 边界。 |
| Monotonic Compaction | 内容状态只允许 `raw -> snipped -> pruned -> summarized`，不恢复原文。 |

## 问题陈述

### 需要解决的问题

当前 `_history` 只追加会产生四类问题：

1. **硬性容量风险**：请求最终会超过模型允许的 Context Window，Provider 返回
   Context Overflow，而 Session 没有恢复路径。
2. **成本与延迟持续增长**：System Message、Tool schema 和完整历史会随每个 Step
   反复发送；历史中的大 Tool Result 会持续计入输入 Token。
3. **注意力质量下降**：过期输出、重复读取和旧的中间过程会占据长上下文中部，降低
   当前目标、最近约束和最新工具证据的相对可见性。
4. **压缩正确性约束尚未建模**：Tool Call 与 Tool Result 必须按 `call_id` 配对；
   任意消息截断、并行调用拆分或在 Active Turn 中间生成摘要都可能形成非法请求。

### 现有证据

- 代码事实：Agent Loop 的 History 没有删除或替换操作。
- 代码事实：Provider 已返回 usage，但 Runtime 不保存它。
- 代码事实：单个 Execution 可保留 1 MiB 数据，一次默认返回 200 个输出块。
- 研究证据：模型对长上下文中部信息的使用能力可能下降。
- 行业实践：Tool Result clearing 与历史摘要通常是两个不同成本等级的操作。

当前尚无 cli-agent 自身的长程任务 benchmark，因此 60%、80%、95%、保护区大小和
回收目标应视为初始策略参数，而不是已经由本项目数据验证的最终常量。

### 不处理的影响

- 交互 Session 的最大可用寿命由模型 Context Window 被动决定。
- 工具密集任务可能在同一个 User Turn 的中间失败，无法产出最终答案。
- Host 无法观察 Context Pressure、压缩动作、释放量或估算来源。
- 后续若分别在 Provider、Agent Loop 和 Tool handler 中加入局部截断，将形成多套
  互不一致的 Context 语义和测试边界。

## 目标与非目标

### 目标

1. 为每个 Session 建立唯一的 Context History 所有者。
2. 在每次普通 Model Request 前检查并按需压缩，不等待 User Turn 结束。
3. 保持 System Message、Active Turn、最近完整 Turn 和 Tool Call 配对合法。
4. 让 Tier 1/2 使用确定性操作处理旧的成功 Tool Result，不调用 LLM。
5. 让 Tier 3 使用无 Tools 的增量摘要，且成功后原子提交。
6. 将 Provider usage 与具体 Context Revision 关联，区分 Reported 与 Estimated。
7. 在发送已知超预算请求前压缩或返回明确错误，不静默删除 User 指令。
8. 保持不同 Session 的 History、usage、summary frontier 和压缩状态完全隔离。
9. 公开不含消息正文的压缩观测信息，以便测试、调优和 Host 展示。
10. 不保留旧的无界 `_history` 执行路径或双写兼容层。

### 非目标

1. 实现跨 Session 的长期记忆、向量检索或持久化用户画像。
2. 将完整原始 Transcript 作为审计日志持久化到磁盘或数据库。
3. 第一版截短 User Message 中的 Markdown 代码块。
4. 第一版按句子、段落或字符数量截断普通 Assistant 文本。
5. 根据语义相关性、Embedding 或模型评分选择单条消息。
6. 为不同供应商实现服务端 compaction Adapter。
7. 为每个供应商维护完整的 model-name 窗口表；仅内置少量已知模型（当前
   `deepseek-v4-flash` = 1M），其余模型必须由 Host 显式配置。
8. 让摘要请求执行 Tool、修改 Workspace 或进入 Environment Kernel。
9. 保证摘要无信息损失；摘要是受约束但仍有损的最后防线。

### 成功标准

- [ ] 每个普通 Model Request 都由 `prepare_request()` 产生。
- [ ] Tier 1/2 的 Provider 调用次数始终为零。
- [ ] 任意压缩后，所有 Tool Result 都有且只有一个对应 Tool Call。
- [ ] Protected Suffix 从完整 User Turn 边界开始，Active Turn 不进入普通摘要。
- [ ] Tier 3 失败时，原 History、旧摘要和 Summary Frontier 均不改变。
- [ ] 对已知超过 Input Budget 的请求，不调用普通 Provider generation。
- [ ] Provider 报 Context Overflow 时最多强制压缩并重试一次。
- [ ] Context Entry 的压缩状态不可逆且重复 prepare 具有幂等性。
- [ ] 两个 Session 的压缩、摘要或失败不会改变彼此的请求。
- [ ] 完整 pytest、Ruff、mypy 和 Context 专项测试通过。

## 设计原则

### 请求前准备，响应后观测

Context Manager 的核心时序是：

```text
append(UserMessage)
        |
        v
prepare_request() ----> Provider.generate(normal request)
                              |
                              v
                        observe(usage)
                              |
                              v
                    append(AssistantMessage)
                              |
                 +------------+------------+
                 |                         |
             no ToolCall                 ToolCall
                 |                         |
             Turn ends              dispatch tools
                                           |
                                           v
                                append(ToolResultMessage)
                                           |
                                           v
                                  prepare_request()
```

Turn 结束后不主动压缩。若用户不再发起下一轮，立即压缩只会产生不必要的字符串操作、
Prompt Cache 失效或 Tier 3 模型调用。下一次用户输入追加后，Context Manager 会在
真正需要发请求之前处理包括上一轮最终回答在内的全部新内容。

### 正确性优先级

发生约束冲突时使用以下优先级：

1. 保持 System/User 指令边界和 Tool Call 协议合法；
2. 不发送已知超过 Input Budget 的普通请求；
3. 保留 Active Turn 和 Protected Suffix；
4. 保留旧 Tool Result 的原始细节；
5. 减少 Provider 调用、Token 成本和 Prompt Cache 失效。

这意味着“保护区永不压缩”不是无限输入下的绝对承诺。若最新的单个成功 Tool Result
本身超过可用预算，Context Manager 可以立即对该可重新读取的结果执行 oversized
snip；如果 User Message、Tool Call 参数或无法重读的核心结果仍然无法装入，则返回
Context Overflow 错误，而不是静默删减。

### 单调内容状态

每个可压缩 Tool Result 的状态只允许沿以下方向变化：

```text
raw -> snipped -> pruned -> summarized
```

摘要覆盖的完整 Turn 会从活动投影中删除，Summary Frontier 只能向后推进。Context
Manager 不保留用于“将来恢复”的原始大文本；它只保留回收统计和必要的运行时定位
信息。需要审计原始 Transcript 时，应由未来独立的 Host 持久化边界解决。

## 评估标准

| 标准 | 权重 | 描述 | 最低阈值 |
|---|---:|---|---|
| 协议正确性 | 25% | 保持 Message 顺序、完整 Turn 和 `call_id` 配对 | 不产生 Provider-invalid History |
| Provider 中立性 | 20% | Runtime 语义不依赖供应商 endpoint 或私有类型 | 当前 Chat Completions Adapter 可实现 |
| 可测试与可观测性 | 15% | 阈值、状态转换、失败和用量来源可确定性验证 | 无网络单测覆盖全部 Tier |
| Context 效率 | 15% | 优先回收大且可重读内容，控制额外模型调用 | Tier 1/2 零 LLM 调用 |
| 可维护性 | 15% | History 所有权、策略和 Provider 调用边界清晰 | 单一 Context 所有者 |
| 实施成本 | 10% | 改动范围、依赖和迁移复杂度 | 不新增 tokenizer/framework 依赖 |
| **总计** | **100%** | | |

评分采用 1-5 分定性估计。项目尚无长程 benchmark，分数用于比较结构性差异，不代表
实测性能结论。

## 选项分析

### 选项 1：在 Agent Loop 中原地改写 History

**描述**

继续让 `AgentLoop` 持有 `list[ModelMessage]`，在构造 `ModelRequest` 前直接扫描并替换
其中的 Tool Result 或旧消息；usage、阈值、摘要和保护区状态也存放在 Agent Loop。

**优点**

- 改动文件少，可以直接复用现有 append/request 循环。
- 不改变 Provider Adapter，也不引入新的 Session component。
- 第一阶段 Tool Result 字符串替换可以较快产生可见效果。

**缺点**

- Agent Loop 同时承担模型流、Tool dispatch、History、预算、压缩和摘要职责。
- 很难独立测试压缩计划而不驱动完整异步 Agent Loop。
- Tier 3 需要从 Agent Loop 内再次调用同一个 Provider，递归和事件流边界容易混淆。
- `history` 属性无法区分原消息、投影消息、摘要 frontier 和 usage revision。
- 后续 Provider-native compaction 或长期记忆会继续扩大 Agent Loop 分支。

**风险**

| 风险 | 可能性 | 影响 | 缓解 |
|---|---|---|---|
| 在 Tool Exchange 中间截断 | 中 | 高 | 增加 Turn scanner，但会进一步扩大 Loop |
| 摘要请求进入正常 Tool dispatch | 中 | 高 | 增加 request kind 分支 |
| 状态测试依赖完整 scripted stream | 高 | 中 | 暴露更多 Agent Loop 私有字段 |

**工作量**：M。

### 选项 2：Session-scoped Context Manager

**描述**

新增 `_ContextManager` 作为 Session 内唯一 History 所有者。Agent Loop 只负责
Append、Prepare、Generate、Observe、Dispatch 的编排。Context Manager 维护
Conversation Turn、Context Revision、usage anchor、压缩状态和 Summary Frontier，
并在需要时通过一个受限 `_ContextSummarizer` 发起无 Tools 请求。

**优点**

- History、预算和压缩状态具有单一所有权，可单独进行纯内存测试。
- 正常 Model Request 与摘要 Model Request 的调用路径明确分离。
- Context Policy 可配置，但无需引入通用 middleware framework 或策略 registry。
- Session 已拥有独立 Agent Loop/Kernel，该组件与现有生命周期一致。
- Provider-native compaction 将来可以在 Adapter 层优化，不改变 Runtime 主语义。

**缺点**

- 需要迁移现有 `AgentLoop.history` 测试和所有 request sequence 断言。
- 需要建立 Turn/Tool Exchange 边界和 request revision 数据模型。
- `prepare_request()` 因 Tier 3 变为异步，接口比直接读取 tuple 更复杂。
- 同一 Session 的并发 `run_turn` 需要明确串行化，否则 Context Revision 会竞态。

**风险**

| 风险 | 可能性 | 影响 | 缓解 |
|---|---|---|---|
| 组件过度抽象 | 中 | 中 | 第一版固定三种操作，不做通用 middleware chain |
| usage 与错误 request revision 关联 | 中 | 高 | Prepared Request 返回不可变 revision token |
| 同 Session 并发 mutation | 中 | 高 | Agent Loop 使用一个 turn lock 串行化 |

**工作量**：L。

### 选项 3：完全委托 Provider 服务端压缩

**描述**

为支持的 Provider 启用 OpenAI Responses compaction 或 Anthropic Context
Management，由服务端根据阈值返回 compacted item；Runtime 不实现本地四级策略。

**优点**

- 供应商可以使用真实 tokenizer、模型内部状态和专用压缩格式。
- 应用侧摘要代码、Prompt 和额外事件较少。
- 对同供应商支持的模型，服务端可能提供更一致的错误与 usage 统计。

**缺点**

- 当前 OpenAI-compatible Chat Completions endpoint 没有统一 compaction contract。
- OpenAI encrypted compaction item 与 Anthropic compaction block 不能互换。
- Runtime 无法统一保证 Tool Result-only Tier 1/2、保护区和四节摘要结构。
- 测试需要模拟多个供应商私有 payload，公共 History 语义不再唯一。
- 切换 Provider 可能改变 Session 可继续性和压缩行为。

**风险**

| 风险 | 可能性 | 影响 | 缓解 |
|---|---|---|---|
| Provider 不支持或改变 beta contract | 高 | 高 | 仍需本地 fallback，形成双实现 |
| 私有 compacted item 无法迁移 | 高 | 中 | Session 固定 Provider，但降低可移植性 |
| Host 无法解释压缩结果 | 中 | 中 | 只能记录供应商统计 |

**工作量**：支持单一 Provider 时 M；覆盖当前 Provider-neutral contract 时 XL。

### 选项比较

| 标准 | 权重 | 选项 1 | 选项 2 | 选项 3 |
|---|---:|---:|---:|---:|
| 协议正确性 | 25% | 3（0.75） | 5（1.25） | 4（1.00） |
| Provider 中立性 | 20% | 5（1.00） | 5（1.00） | 1（0.20） |
| 可测试与可观测性 | 15% | 3（0.45） | 5（0.75） | 3（0.45） |
| Context 效率 | 15% | 4（0.60） | 4（0.60） | 5（0.75） |
| 可维护性 | 15% | 2（0.30） | 4（0.60） | 3（0.45） |
| 实施成本 | 10% | 5（0.50） | 3（0.30） | 4（0.40） |
| **加权总分** | **100%** | **3.60** | **4.50** | **3.25** |

## 建议方案

建议采用选项 2：Session-scoped Context Manager。

该选项在权重最高的协议正确性和 Provider 中立性上满足全部约束，并使水位测试、
压缩状态机、摘要失败和 Session 隔离可以脱离真实网络独立验证。它的实施成本高于
直接修改 Agent Loop，但避免让正常 Model stream 与内部摘要 stream 共用一套隐式
分支。

接受以下取舍：

1. **引入新的 Session component**：Context 是下一阶段的核心领域，单一所有者带来的
   生命周期和测试边界能够抵消一个额外类的成本。
2. **首版仍需估算未测增量**：通用 Chat Completions 没有标准的请求前 count API；
   通过 Reported anchor、保守 delta、安全余量和 overflow 单次恢复降低风险。
3. **Tier 3 使用有损摘要**：只在 95% 且确定性压缩不足时触发，并保留完整最近 Turn。
4. **暂不使用供应商原生 compaction**：先固定 Runtime contract；未来 Adapter 优化
   必须通过相同的行为与评估测试。

采用本方案的条件：

- Context Window 必须来自显式配置或内置模型最大上下文注册表（如
  `deepseek-v4-flash` = 1M），Host 可用 `CLI_AGENT_CONTEXT_WINDOW` 覆盖；
- 摘要请求不得携带 Tools，也不得进入 Agent Loop 的 Tool dispatch；
- 第一版不增加通用 strategy registry、middleware graph 或第三方 tokenizer 依赖；
- 阈值进入默认配置前必须有确定性合成轨迹验证；真实任务 benchmark 用于后续调参。

## 技术设计

### 组件关系

```text
AgentRuntime
  |
  +-- Session
        |
        +-- EnvironmentKernel
        |
        +-- AgentLoop
              |
              +-- _ContextManager
              |     +-- _ContextLedger
              |     +-- _TokenMeter
              |     +-- _ToolResultReducer
              |     +-- _ContextSummarizer
              |
              +-- ModelProvider
```

`_ContextManager` 是 Session-scoped，不进入 `_RuntimeResources`。Runtime Resources
只拥有 Workspace-scoped 的 Catalog、Capability View 和 Tool Environment；Context
History、summary 和 usage 会随 `close_session()` 一起释放。

### 初步接口

```python
@dataclass(frozen=True, slots=True)
class ContextPolicy:
    context_window_tokens: int
    output_reserve_tokens: int
    safety_margin_tokens: int
    protected_tokens: int = 8_000
    snip_threshold: float = 0.60
    prune_threshold: float = 0.80
    summarize_threshold: float = 0.95
    snip_target: float = 0.55
    prune_target: float = 0.70
    summarize_target: float = 0.55
    minimum_reclaim_tokens: int = 4_096
    excluded_tools: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class PreparedContext:
    request: ModelRequest
    revision: int
    pressure: ContextPressure
    operations: tuple[ContextOperation, ...]


class _ContextManager:
    def append(self, message: ModelMessage) -> None: ...

    def observe(self, revision: int, usage: ModelUsage | None) -> None: ...

    async def prepare_request(self) -> PreparedContext: ...
```

具体命名可在实现时按 code style 调整，但必须保留以下合同：

- `append()` 是 Context History 的唯一写入口；
- `observe()` 只能更新与已发请求 revision 对应的 usage anchor；
- `prepare_request()` 是普通 Model Request 的唯一构造入口；
- Tier 3 可以 await 内部摘要，但摘要请求不再调用 `prepare_request()`。

`ModelRequest.tools` 改为可显式构造的字段，普通请求继续默认使用
`BUILT_IN_SYSCALL_SCHEMAS`，摘要请求使用空 tuple。该变更不保留禁止传入 Tools 的旧
constructor 兼容测试。

### Context Policy 校验

构造时至少校验：

```text
0 < snip_threshold < prune_threshold < summarize_threshold < 1
0 < target < corresponding threshold
output_reserve_tokens > 0
safety_margin_tokens >= 0
protected_tokens >= 0
context_window_tokens > output_reserve_tokens + safety_margin_tokens
minimum_reclaim_tokens >= 0
```

Reference CLI 必须取得 Context Window：未显式配置时从内置模型最大上下文注册表
解析（当前仅 `deepseek-v4-flash` = 1M），模型不在注册表且未显式配置时返回明确
配置错误。其他阈值先使用 RFC 默认值，并允许 Host 构造 `ContextPolicy` 覆盖。

### Input Budget 与水位

```text
input_budget = context_window_tokens
               - output_reserve_tokens
               - safety_margin_tokens

pressure = projected_input_tokens / input_budget
```

水位计算使用“下一次普通请求的预计输入”，不是上一请求的
`usage.total_tokens / context_window_tokens`：

- `total_tokens` 同时包含输出 Token，部分 Provider 还可能把不会进入下一次 History
  的隐藏推理 Token 计入输出；
- 上一请求的 `input_tokens` 是已发送 request revision 的真实输入锚点；
- Completion Message、Tool Result 和新 User Message 是该 revision 之后的 delta；
- Tool schema 和 System Message 必须包含在第一次估算以及后续请求投影中。

Token Meter 按以下优先级取值：

1. Provider 有精确 count hook 时，直接计算完整投影；
2. 否则使用最近一次 Reported `input_tokens` 加 revision delta 的保守估算；
3. Session 第一次请求没有 Reported anchor 时，对完整序列化 request 做保守估算；
4. 所有观测结果标明 `reported`、`counted` 或 `estimated` 来源。

第一版不引入通用 tokenizer 依赖。保守估算的具体公式应通过独立测试固定，并保留
足够安全余量；不得在诊断中把 Estimated 数字描述为 Provider-reported 真值。

### Turn 和 Tool Exchange 边界

Context Ledger 从 Message 顺序派生 User Turn：

```text
UserMessage
  AssistantMessage(tool calls A, B)
  ToolResultMessage(results A, B)
  AssistantMessage(tool call C)
  ToolResultMessage(result C)
  AssistantMessage(no tool call)  # closes the Turn
```

边界规则：

- System Message 独立存在，永不进入压缩候选；
- Assistant Tool Call 和全部对应 Tool Result 构成不可拆分 Tool Exchange；
- 并行调用必须验证 `call_id` 集合完全相等，不按 ready event 顺序推断；
- 没有 Tool Call 的 Assistant Message 正常关闭当前 Turn；
- stream 中断、Provider 异常或 Tool Result 尚未追加时，Turn 保持 Active；
- Tier 3 只消费已关闭的完整 Turn；
- Tier 1/2 可以修改完整 Turn 内的 Tool Result payload，但不能删除 Tool Result
  Message 或改变 `call_id`。

Context Manager 遇到内部不合法 History 时应返回内部错误并发送诊断，不尝试通过删除
孤立消息“修复”它。

### Protected Suffix

Protected Suffix 从 History 末尾向前累计预计 Token，并向外扩展到完整 Turn 边界。
初始大小建议为：

```text
min(policy.protected_tokens, input_budget * 20%)
```

以下内容无条件包含在普通 Protected Suffix 中：

- Active Turn；
- 最近一个完整 User Turn；
- 达到 protected token 目标所需的其他完整 Turn。

Tier 1/2 不处理 Protected Suffix 内的普通结果，Tier 3 不摘要它。唯一例外是 oversized
success Tool Result：如果它单独使下一次请求无法进入 Input Budget，并且 Runtime
确认该结果可通过 Session 内 execution state 重新读取，可以在 Active Turn 中进行
有界 snip。User Message、Tool Call 参数和错误结果不使用此例外。

### Tier 0：静默期

触发条件：`pressure < 0.60`。

行为：

- 不扫描和改写可压缩 payload；
- 不调用摘要模型；
- 只返回当前 History 投影及水位观测；
- append 与 observe 仍正常推进 revision 和 usage anchor。

### Tier 1：Snip

触发条件：`pressure >= 0.60`。

执行顺序：从 Protected Suffix 之前最旧的 `raw`、成功且允许压缩的 Tool Result 开始，
逐个转换为 `snipped`，直到预计 pressure 低于 `snip_target`、无候选项或预计回收不足
最小回收量。

对当前 Execution snapshot，Snip 应保留：

- `call_id`；
- Tool 名和必要调用识别信息；
- `exec_id`、`status`、`exit_code`、`is_terminal`；
- Cursor、`truncated` 和可重读范围；
- 有界的开头与结尾输出；
- 省略的 chunk/byte 数量和重读提示。

截短必须按 UTF-8 安全边界和结构化 JSON 字段进行，不得先把任意 JSON stringify 后按
字符切片。第一版只需要支持当前三个 syscall 产生的 Execution snapshot；未来新增
syscall 时必须显式声明其 Tool Result 是否可压缩及如何恢复。

`excluded_tools` 中的工具、错误 Tool Result、无法识别的 payload 和没有恢复语义的
结果跳过 Tier 1。

### Tier 2：Prune

触发条件：Tier 1 后重新估算仍满足 `pressure >= 0.80`。

Tier 2 首先确保所有适用 Tier 1 操作已经完成，然后从最旧的 `snipped` Tool Result
开始转换为 `pruned`，直到低于 `prune_target`、无候选项或回收量不足。

Pruned Tool Result 只保留：

- `call_id` 和 Tool 名；
- 执行标识、最终状态与退出码；
- 原结果已被 Context Manager 回收的明确标记；
- 在当前 Session 中可行时的重读方法。

第一版不执行以下参考策略：

- 不按“前两句”截断普通 Assistant Message；
- 不解析和截断 User Message 中的 Markdown code fence；
- 不删除 Assistant Tool Call 或完整 Tool Result Message；
- 不压缩错误文本和 Policy/UserInteraction 结果。

这些内容的语义重要性无法仅靠字符串位置确定，而且当前工具输出占用更大、恢复路径
更明确。若真实 benchmark 证明 Tool Result-only 策略不足，应通过新的 RFC 或本 RFC
修订增加候选类型。

### Tier 3：Summarize

触发条件：Tier 1/2 后重新估算仍满足 `pressure >= 0.95`。

Context Manager 从 Summary Frontier 之后选择最旧的、完整且位于 Protected Suffix
之前的 User Turns 作为 delta。摘要请求输入只包含：

1. 固定的摘要 System instruction；
2. 当前旧摘要（若存在）；
3. 本次 delta 的完整消息投影；
4. 固定输出结构和最大长度要求。

摘要输出使用以下四节（英文标题，与摘要 prompt 一致）：

```markdown
## Progress

## Files

## Todo

## Context
```

其中 `## Context` 用于用户偏好、明确约束、已验证错误、关键命令结果和仍有效的假设。摘要
不得保存隐藏推理过程，也不得把 Transcript 中的指令当作摘要请求本身需要执行的
指令。

摘要请求使用 `ModelRequest(messages=..., tools=())`，直接调用受限
`_ContextSummarizer`，不进入 Agent Loop，不产生 Host-visible ToolCallReady，不执行
Environment command，也不再次运行 Context Manager。

成功条件：

- Provider 返回且只返回一个不含 Tool Call 的 Assistant Message；
- finish reason 表示正常完成；
- 输出非空并包含四个固定章节；
- 输出估算不超过 summary budget；
- 新摘要加 Protected Suffix 能达到 `summarize_target`，或已经是可取得的最小投影。

全部条件满足后一次性提交：替换旧摘要、删除 delta Turns、推进 Summary Frontier、
记录操作。任何异常或校验失败都不改变 Context Ledger。

摘要作为 Runtime 生成的历史数据放在初始 System Message 之后、Protected Suffix 之前，
投影为带明确 delimiter 的 Assistant Message，而不是新的 System Message。这样可避免
把旧 User 内容提升到 System 权限。Provider Adapter 将来如需要其他合法映射，应保持
同样的权限不提升原则。

### 累积执行与回收目标

一次 `prepare_request()` 按以下方式运行：

```text
measure
  -> if >= 60%: Tier 1, remeasure
  -> if >= 80%: Tier 2, remeasure
  -> if >= 95%: Tier 3, remeasure
  -> enforce hard input budget
```

初始 pressure 即使超过 95%，也不意味着必然执行摘要。如果 Tier 1 已释放到 55%，
本次 prepare 就结束。该规则符合“成本递增”：只有低成本策略不足时才进入更高 Tier。

触发线与回收目标之间保留 hysteresis，避免每次新消息都重复修改旧 prefix。初始建议：

| Tier | 触发 | 回收目标 |
|---|---:|---:|
| Snip | 60% | 55% |
| Prune | 80% | 70% |
| Summarize | 95% | 55% |

每次实际修改还应满足 `minimum_reclaim_tokens`。可参考
`max(4_096, input_budget * 5%)` 计算动态最小值，但最终默认值需要 benchmark 验证。

### Prompt Cache 行为

修改旧消息会使其后的 Prompt Cache prefix 失效，完全避免是不可能的。本 RFC 通过
以下规则摊薄成本：

- 内容状态不可逆，不在后续请求中来回恢复和再次截短；
- 从最旧候选开始，未被修改的 System prefix 保持稳定；
- 每次压缩必须满足最小回收量；
- Tier 3 一次回收到较低目标，而不是频繁更新小摘要；
- Turn 结束不提前压缩，延迟到下一次真正需要请求时执行。

如果 Provider 可报告 cached input tokens，可以在未来将其增加为可选的
Provider-neutral usage detail；本 RFC 第一阶段不要求扩展公共 usage 字段。

### Context Overflow 恢复

Provider Adapter 应把可识别的 Context Window exceeded 响应映射为一个稳定的
`ModelContextOverflowError`，而不是只暴露通用 HTTP error。

普通请求发生该错误时：

1. Context Manager 使当前 Reported anchor 失效；
2. 强制执行所有仍可用的 Tier 1/2，并在存在完整 prefix 时运行 Tier 3；
3. 重新检查 hard budget；
4. 只重试原普通 Model Step 一次；
5. 第二次 overflow 或无候选项时向 Host 返回稳定错误，Session 保持可关闭状态。

该重试发生在 Provider 接受并产生 Assistant Completion 之前，因此不会重复执行 Tool。
摘要调用本身发生 Context Overflow 时不递归恢复；它按摘要失败处理。

### 同 Session 串行化

Context Manager 是单写者状态机。`AgentRuntime.run_turn()` 对同一个 `session_id` 的并发
调用必须通过 Session-owned lock 串行化完整 User Turn。不同 Session 继续并发，且各自
拥有独立 lock、Context Manager、Kernel 和 Provider request sequence。

如果 Host 取消正在运行的 Turn，Context Manager 保留已经成功追加的消息和已经提交的
单调压缩结果；未完成的 Tier 3 原子更新不会提交。后续是否允许 Host 重试未完成 Turn
沿用现有 Agent Loop stream failure 语义，不在本 RFC 中增加自动消息删除。

### 观测

每次发生实际压缩时产生一个不含正文的结构化 Context operation：

```text
session_id
revision_before / revision_after
tier
usage_source
input_tokens_before / input_tokens_after
entries_changed
turns_summarized
summary_input_tokens / summary_output_tokens
reason: watermark | oversized_result | overflow_recovery
```

不得记录 User/Assistant/Tool Result 正文、摘要正文、环境变量、命令输出或 Secret。
Host-visible 表面是扩展 `ModelEvent` 还是使用 `RuntimeDiagnostic`，留作评审问题；内部
数据合同和测试断言不依赖最终展示通道。

## 安全与隐私

| 风险 | 影响 | 可能性 | 缓解措施 |
|---|---|---|---|
| 摘要把旧 User 指令提升为 System 权限 | 高 | 中 | 摘要投影为有 delimiter 的 Assistant 历史数据，不新增 System Message |
| Transcript 中的 Prompt Injection 控制摘要模型 | 高 | 中 | 固定 System instruction 声明 Transcript 是不可信数据；无 Tools；结构校验 |
| 摘要遗漏审批、Policy 或用户约束 | 高 | 中 | Protected Suffix；四节结构；确定性事实保留 eval；失败不提交 |
| Tool Call/Result 配对被拆坏 | 高 | 中 | 完整 Tool Exchange 原子边界；集合校验；协议 contract tests |
| 跨 Session History 泄漏 | 高 | 低 | Context Manager 由 `_Session` 独占，不进入 Runtime-wide resources |
| 压缩诊断泄漏命令输出或 Secret | 高 | 低 | 只记录计数和枚举，不记录正文、参数、摘要或环境值 |
| 被删 Tool Result 无法重读 | 中 | 中 | 只有声明可恢复的成功结果进入 Tier 1/2；保留 exec_id；否则跳过 |
| 估算偏小导致超限 | 中 | 中 | 安全余量、Adapter overflow 分类、一次强制压缩重试 |
| 估算偏大导致过早压缩 | 中 | 中 | Reported anchor 校准；标记来源；真实轨迹 benchmark 调参 |

Context Summary 与 History 保持 Session 内存生命周期，Session close 后释放。本 RFC
不新增持久存储、远程日志或跨 Session memory，因此不改变当前数据保留边界。

## 实施计划

### 阶段 0：基线与评估轨迹

- 固化当前 Agent Loop、Provider usage、并行 Tool Call 和多 Session 请求序列。
- 增加使用小 Context Window 和确定性 token meter 的合成长程轨迹。
- 定义必须保留的事实集合：用户约束、文件状态、失败原因、待办和 exec_id。
- 记录无压缩基线的 request token 曲线、Provider 调用次数和最终任务结果。

验收：测试可稳定复现 60%、80%、95% 和 hard overflow，不依赖真实网络。

### 阶段 1：Context Budget 与 Model Request 合同

- 新增 `ContextPolicy` 和参数校验。
- 让 `ModelRequest` 可以显式传入空 Tools。
- 将 Provider usage 与 immutable request revision 关联。
- 增加 `ModelContextOverflowError` provider-neutral 类型。
- Reference CLI 接受显式 Context Window 和预算配置。

验收：普通请求行为不变；无 Tools 请求 payload 不发送 Tool schema；错误配置 fail fast。

### 阶段 2：Context Ledger 与 Agent Loop 时序

- 新增 `_ContextManager`、`_ContextLedger` 和 `_TokenMeter`。
- 将 `_history` 从 Agent Loop 迁移到 Context Manager，不保留双写。
- 实现 Turn 和 Tool Exchange 验证。
- 在每次普通 request 前调用 prepare，response 后 observe。
- 为同 Session 的完整 `run_turn` 增加串行 lock。

验收：未启用任何压缩候选时，请求序列与当前行为相同；不同 Session 仍可并发。

### 阶段 3：Tier 1/2 Tool Result 压缩

- 实现当前 Execution snapshot 的 schema-aware reducer。
- 实现 `raw -> snipped -> pruned` 单调状态。
- 实现 Protected Suffix、excluded tools、minimum reclaim 和 oversized result guard。
- 增加 operation stats，但不记录正文。

验收：Tier 1/2 零 LLM 调用；call_id、执行状态和重读信息完整；重复 prepare 幂等。

### 阶段 4：Tier 3 增量摘要

- 实现 `_ContextSummarizer` 的无 Tools Provider 调用。
- 固定四节摘要 prompt 和输出校验。
- 实现旧摘要加 delta、完整 Turn 选择和 Summary Frontier。
- 实现原子提交、失败保留和 summary budget。

验收：摘要失败、超时、Tool Call、空输出、缺章节和 overflow 都不改变现有 History。

### 阶段 5：Overflow 恢复、观测与文档

- 映射当前 OpenAI-compatible Provider 的 Context Overflow。
- 普通 Step 实现最多一次强制压缩重试。
- 确定 Host-visible operation event 或 RuntimeDiagnostic 通道。
- 更新 `docs/architecture.md`、README、CLI 配置和 Context 策略说明。
- 运行完整 pytest、Ruff、mypy 和 diff check。

验收：合成 overflow 轨迹不会重复 Tool execution；不可恢复时错误稳定且 Session 可关闭。

### 建议里程碑与子 Issue

里程碑标题：`feat(context): implement four-tier context compaction`

建议拆分：

1. `feat(model): define context budgets and token measurements`
2. `feat(context): own conversation history in a context ledger`
3. `feat(context): snip and prune stale tool results`
4. `feat(context): summarize completed historical turns`
5. `test(context): prove compaction safety and observability`

Issue 正文应按项目约定使用中文，初始状态为 `pending`。在 RFC 通过 peer review 前不
将任何实现 Issue 标记为 `resolved`。

## 测试策略

### Budget 与水位

- 精确覆盖低于、等于和高于 60%、80%、95% 的边界。
- 覆盖 output reserve、安全余量和非法阈值顺序。
- 覆盖 Reported、Counted、Estimated 三种来源及 stale revision rejection。
- 覆盖第一次请求无 usage anchor、usage 为 `None` 和 Provider 返回零值。
- 验证每一级执行后重新测量，不因初始 95% 无条件调用摘要。

### Turn 与 Tool 协议

- 单 Tool Call、多个串行 Tool Call 和并行 Tool Call。
- ToolCallReady 顺序与 Assistant Message 中顺序不同。
- 缺失、多余、重复和跨 Turn `call_id` 都产生稳定内部错误。
- cutoff 落在并行 Tool Results 中间时向外扩展，不拆分 exchange。
- Active Turn、stream failure 和 Tool dispatch failure 不进入 Tier 3。

### Tier 1/2

- 长 stdout、长 stderr、混合 stream、无换行大块和多字节 UTF-8。
- 保留 head/tail、执行状态、Cursor、退出码、省略量和重读提示。
- error、excluded tool、未知 payload 和 Protected Suffix 保持不变。
- oversized current Tool Result 可 snip，oversized User Message 返回错误。
- 状态单调、重复 prepare 幂等、回收不足不破坏 cache prefix。

### Tier 3

- 首次摘要、旧摘要加 delta 和多次 frontier 推进。
- 只选择完整、已关闭且位于 Protected Suffix 之前的 Turns。
- 摘要请求 `tools == ()`，任何 Tool Call 都视为失败。
- 空输出、缺章节、异常、中断、overflow 和超预算输出全部原子失败。
- 摘要中的文件、约束、失败、待办和用户偏好进入下一次普通请求。
- 摘要作为 Assistant 数据投影，不增加第二个 System Message。

### Runtime 集成

- 一个 User Turn 内多个 Model Step，每个请求前都执行 prepare。
- Turn 最终回答后不立即压缩，下一次 User Message 后再 prepare。
- 同 Session 并发 run_turn 被串行化，不同 Session 继续并发。
- Provider overflow 只重试一次，不重复 Tool dispatch。
- close_session 释放 Context Manager；复用同 ID 创建全新 Context。
- Host operation 事件不包含任何原始消息或 Tool payload。

### 真实任务评估

在合成 contract tests 通过后，建立至少三类长程轨迹：

1. 文件探索与多次局部读取；
2. 长命令输出和重复 `output` 轮询；
3. 多轮修改、测试失败、修复和最终验证。

比较无压缩、Tool Result-only、完整四级策略的：

- 任务完成率和最终答案完整性；
- 必须保留事实的召回；
- 每个 Step 的 input tokens 和 Context Pressure；
- 总 Provider 调用、摘要调用、延迟和费用；
- Context Overflow 次数；
- cached input tokens（Provider 可用时）。

在有项目数据前不为质量回归设定缺乏依据的小数阈值。发布至少要求所有确定性约束
事实在评估集中保留，且四级策略不引入新的 Tool 协议错误或未恢复 overflow。

## 发布、回滚与迁移

该变更按项目约定不保留旧 `_history` 兼容路径。阶段实现完成后，Agent Loop 只通过
Context Manager 构造普通请求，不设置同时维护两份 History 的 feature flag。

发布前回滚以整体 revert 未合并 milestone 为主。若实现已进入分支但评估未通过，应
回滚 Context Manager、Model Request contract、CLI 配置和测试，不只关闭 Tier 3，
否则会留下两种 usage revision 和 History 所有权语义。

Session Context 当前不持久化，因此没有数据迁移。已运行进程中的 Session 不跨版本
恢复；进程重启后按新实现创建 Context Manager。

## 风险与缓解

| 风险 | 影响 | 可能性 | 缓解措施 |
|---|---|---|---|
| 四级默认阈值不适合所有模型 | 过早压缩或超限 | 高 | 显式 ContextPolicy；合成边界测试；真实轨迹调参 |
| 摘要产生语义漂移 | 后续决策基于错误历史 | 中 | 最后触发；四节结构；Protected Suffix；事实 eval；原子失败 |
| Tool Result reducer 绑定当前 snapshot shape | 新 syscall 无法安全压缩 | 中 | 未声明类型默认不压缩；新增 syscall 必须定义 retention policy |
| Context Manager 变成通用 middleware framework | 维护复杂度增加 | 中 | 第一版固定 append/observe/prepare 和三种操作 |
| 保守估算造成 CJK/代码过度压缩 | Token 利用率下降 | 中 | Reported anchor 校准；明确来源；未来 Provider count hook |
| Prompt Cache 因旧 prefix 修改失效 | 成本短期升高 | 高 | minimum reclaim、hysteresis、单调状态、延迟压缩 |
| 同 Session 并发行为改变 | Host 请求需要等待 | 中 | 明确单写者合同；不同 Session 不受影响；增加调度测试 |
| 摘要额外占用 Provider rate limit | Tier 3 延迟或失败 | 中 | 仅 95% 触发；回收到 55%；失败不提交；可观测摘要调用 |

## 未决问题

1. **Context Window 配置应使用哪个 Host/CLI 表面？**
   - 已解决：Reference CLI 通过 `CLI_AGENT_CONTEXT_WINDOW` 显式覆盖，未设置时从
     内置模型最大上下文注册表解析；`CLI_AGENT_OUTPUT_RESERVE` 默认 16384、
     `CLI_AGENT_CONTEXT_SAFETY_MARGIN` 默认 4096。
   - Owner：project owner。
   - 状态：Resolved。

2. **摘要第一版是否允许注入不同的 Model Provider？**
   - 使用同一 Provider 改动较小、能力一致；较小模型可能降低成本但增加配置和质量差异。
   - 本 RFC 默认同 Provider、无 Tools；独立 Provider 是否进入首个 milestone 待评审。
   - Owner：project owner。
   - 状态：Open。

3. **Context operation 使用 ModelEvent 还是 RuntimeDiagnostic？**
   - ModelEvent 能按 stream 顺序呈现，但会扩大公共 Model event union。
   - RuntimeDiagnostic 已有 Host callback，但当前主要用于 Runtime/Capability 失败通知。
   - Owner：project owner。
   - 状态：Open。

4. **默认 protected tokens 和 minimum reclaim 如何定值？**
   - 8,000、20% 和 4,096 是设计起点，不是项目实测结果。
   - 阶段 0/5 benchmark 后在 RFC review 中固定最终默认值。
   - Owner：implementation reviewer。
   - 状态：Open。

5. **未来是否保留原始 Transcript 用于审计或恢复？**
   - 当前设计有意只保留活动投影，避免引入持久存储和双历史所有权。
   - 如 Host 需要审计，应独立设计 append-only event sink、数据保留和 Secret policy。
   - Owner：future RFC。
   - 状态：Deferred。

## 决策记录

| 日期 | 状态 | 说明 |
|---|---|---|
| 2026-08-04 | DRAFT | 提出 Session-scoped Context Manager、请求前 prepare、响应后 observe，以及 Tool Result-first 四级水位线方案。 |
| 2026-08-04 | APPROVED | project owner 同意方案，进入 milestone 与子 Issue 拆解。 |
| 2026-08-04 | AMENDED | project owner 修订：Context Window 改为可选环境变量，未设置时从内置模型最大上下文注册表解析（`deepseek-v4-flash` = 1M）；`CLI_AGENT_OUTPUT_RESERVE` 默认 16384。 |
| 2026-08-04 | AMENDED | project owner 修订：摘要四节标题改为英文 `## Progress` / `## Files` / `## Todo` / `## Context`，摘要 prompt 全程英文。 |
| 2026-08-04 | COMPLETED | 全部实现子 Issue（01-06）通过 peer review，四级水位线、overflow 恢复与观测已落地并有端到端验证。 |

project owner 已批准本 RFC。新建实现 Issue 的初始状态仍为 `pending`；每个 Issue 的
实现通过 peer review 后才能标记为 `resolved`。

## 参考资料

### 项目文档

- [四级水位线上下文压缩方案](../../references/上下文压缩策略.md)
- [RFC-0003: Tool Capability Commands](RFC-0003-tool-capability-commands.md)
- [RFC-0006: Explicit Runtime Resource Ownership](../proposed/RFC-0006-explicit-runtime-resource-ownership.md)
- [RFC-0007: Unified Command Routing and Execution Refactor](../proposed/RFC-0007-unified-command-routing-and-execution-refactor.md)
- [Project Architecture](../../architecture.md)

### 外部资料

- [Lost in the Middle: How Language Models Use Long Contexts](https://arxiv.org/abs/2307.03172)
- [Anthropic Context Editing](https://platform.claude.com/docs/en/build-with-claude/context-editing)
- [Anthropic Compaction](https://platform.claude.com/docs/en/build-with-claude/compaction)
- [OpenAI Compaction](https://developers.openai.com/api/docs/guides/compaction)
- [LangChain Short-term Memory](https://docs.langchain.com/oss/python/langchain/short-term-memory)
- [LangMem parallel Tool Call summarization boundary issue](https://github.com/langchain-ai/langmem/issues/126)
