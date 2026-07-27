# feat(runtime): assemble a System Message for each Session / 为每个 Session 组装 System Message

**Status / 状态：** pass

## English

### Background

Representing `SystemMessage` is not enough to make a real model usable. The model also needs a stable description of its role, the bound Workspace, the Runtime's built-in tools, and the operating conventions for completing a task.

Prompt assembly belongs to `AgentRuntime`, which owns the model loop and Workspace-scoped Agent behavior. The Environment Kernel must not generate model instructions, the Provider Adapter must only encode them, and the Reference CLI must not create behavior unavailable to other hosts.

### Changes

- Add a small deterministic System Message assembler owned privately by the Runtime.
- Assemble one ordered System Message from the Runtime's canonical instructions and an optional host instruction supplied through the public `AgentRuntime` opening boundary.
- Include only the current contract: Agent role, bound Workspace and default working-directory semantics, the `exec`, `output`, and `kill` built-in tools, and basic inspect-act-verify behavior.
- Do not duplicate the tools' JSON schemas in prompt text or claim that the Workspace is an operating-system security boundary.
- Snapshot the assembled System Message when a Session is first created and initialize that Session's Conversation History with it exactly once.
- Keep the same System Message at the start of every model request for the lifetime of that Session; closing and recreating the Session produces a fresh snapshot.
- Keep provider role names, wire payloads, credentials, environment dumps, file indexes, and future Skills, Library, or MCP capabilities out of the assembler.
- Add tests through public `AgentRuntime` and `ScriptedModelProvider` proving first-message ordering, multi-turn stability, optional host instruction composition, and fresh assembly after Session recreation.

## 中文

### 背景

仅仅能够表示 `SystemMessage`，还不足以让真实模型可用。模型还需要一份稳定的上下文，用于说明自身角色、绑定的 Workspace、Runtime 的内置工具，以及完成任务时应遵循的工作约定。

Prompt 组装属于 `AgentRuntime`：它持有模型循环，并负责 Workspace 范围内的 Agent 行为。Environment Kernel 不应生成模型指令，Provider Adapter 只应负责编码，Reference CLI 也不能创造其他宿主无法使用的行为。

### 变更

- 添加一个由 Runtime 私有持有、小而确定性的 System Message 组装器。
- 通过公共 `AgentRuntime` 打开边界接收可选的宿主指令，并将其与 Runtime 的规范指令按固定顺序组装为一条 System Message。
- 只包含当前契约：Agent 角色、绑定的 Workspace 与默认工作目录语义、`exec`、`output`、`kill` 三个内置工具，以及基本的检查—执行—验证工作方式。
- 不在 prompt 文本中重复工具 JSON schema，也不宣称 Workspace 是操作系统级安全边界。
- 在 Session 首次创建时生成 System Message 快照，并且只将它初始化到该 Session 的 Conversation History 一次。
- 在 Session 的整个生命周期内，让每次模型请求都以同一条 System Message 开始；关闭并重新创建 Session 时生成新的快照。
- 不把供应商 role 名称、wire payload、凭据、环境变量转储、文件索引以及未来的 Skills、Library 或 MCP 能力放入组装器。
- 通过公共 `AgentRuntime` 和 `ScriptedModelProvider` 添加测试，验证首条消息顺序、多轮稳定性、可选宿主指令组合，以及 Session 重建后的重新组装。
