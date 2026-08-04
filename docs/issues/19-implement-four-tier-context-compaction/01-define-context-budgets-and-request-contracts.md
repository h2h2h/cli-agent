# feat(model): define context budgets and request contracts

**状态：** resolved

## 背景

[RFC-0010](../../rfcs/approved/RFC-0010-session-context-compaction.md) 要求
Runtime 在每次普通 Model Request 前根据明确的 Input Budget 判断 Context Pressure。
当前配置只包含 model、base URL 和 API key，既不知道模型 Context Window，也没有
输出预留或安全余量；`ModelRequest.tools` 还固定为三个 syscall 且不可覆盖，无法表达
Tier 3 所需的无 Tools 摘要请求。

Context Window 不能根据任意 OpenAI-compatible model name 猜测。不同 endpoint 可以
对同名模型配置不同限制，通用 Adapter 也没有标准的模型 metadata 或请求前精确
count API。因此第一步必须先固定预算合同和可表达普通/摘要请求的
Provider-neutral request shape。用户只配置 model name，Reference CLI 从内置的
模型最大上下文注册表解析 Context Window（如 `deepseek-v4-flash` = 1M），需要时
可用环境变量覆盖。

## 影响

完成后，Host 和 Reference CLI 能明确声明一个 Session 的 Context Window、输出预留、
安全余量与四级阈值；未显式配置时 Context Window 从模型注册表解析、输出预留与
安全余量使用内置默认值。Runtime 可以计算稳定的 Input Budget，并能构造携带内置
Tools 的普通请求或 `tools=()` 的内部摘要请求。后续 Context Manager 不需要读取
环境变量、猜测模型规格或绕过 `ModelRequest` 构造器。

## 变更

- 新增不可变、Host-visible 的 `ContextPolicy`，至少包含：
  - `context_window_tokens`、`output_reserve_tokens`、
    `safety_margin_tokens`；
  - 60%/80%/95% 三个触发阈值；
  - Snip/Prune/Summarize 回收目标；
  - Protected Suffix、最小回收量和 excluded tools 配置。
- 在构造时验证：
  - Context Window、输出预留和安全余量形成正的 Input Budget；
  - `0 < snip < prune < summarize < 1`；
  - 每个回收目标严格低于对应触发阈值；
  - Token 数、最小回收量和保护区均为非负或正值的合法组合。
- 通过 `cli_agent.runtime` 导出 `ContextPolicy`，让 `AgentRuntime.open` 接收明确策略；
  内置模型最大上下文注册表（当前仅 `deepseek-v4-flash` = 1M），不保留隐式无界
  Context 路径。
- Reference CLI 配置增加：
  - 可选 `CLI_AGENT_CONTEXT_WINDOW`，未设置时从模型最大上下文注册表解析；
    模型不在注册表且未显式配置时返回明确配置错误；
  - 可选 `CLI_AGENT_OUTPUT_RESERVE`，默认值 16384，固定在配置测试中；
  - 可选 `CLI_AGENT_CONTEXT_SAFETY_MARGIN`，默认值固定在配置测试中；
  - 非整数、负数、零 Input Budget 和非法阈值在 Runtime open 前 fail fast。
- 将 `ModelRequest.tools` 改为可显式构造的不可变 tuple：
  - 普通请求省略参数时仍使用 `BUILT_IN_SYSCALL_SCHEMAS`；
  - 内部请求可明确传入 `tools=()`；
  - Provider 对空 Tools 不发送 `tools` 字段，避免部分兼容 endpoint 拒绝空数组。
- 更新公共表面、CLI 配置、OpenAI-compatible payload 和 scripted provider 测试；
  删除“`ModelRequest` 禁止传入 tools”这一旧契约，不保留兼容 shim。

## 验收标准

- [ ] Context Policy 对所有合法/非法预算与阈值组合有确定性测试。
- [ ] Input Budget 只由显式配置或内置模型注册表计算，不按 endpoint 猜测。
- [ ] 普通 Model Request 仍投影三个 syscall，摘要 request 可以完全不携带 Tools。
- [ ] Reference CLI 只配置 model name 时能解析 Context Window，模型不在注册表
      且未显式配置时返回明确配置错误。
- [ ] 未新增 tokenizer、Agent framework 或模型 metadata 依赖。

