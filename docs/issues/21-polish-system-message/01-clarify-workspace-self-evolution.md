# refactor(prompt): clarify Workspace self-evolution

**状态：** resolved

## 背景

当前 system message 已覆盖 Tool、Skill、Library、文件操作和执行管理，但主要以
分散的命令说明呈现。模型无法从中清晰理解 `.workspace` 是当前 Workspace 的统一、
持久化资源与工具目录，也缺少对自主维护和能力沉淀的明确指引。因此，现有能力虽已
具备，模型仍可能把 `.workspace` 当作只读配置或宿主提供的临时资源。

## 影响

完成后，模型将把 `.workspace` 视为自己在当前 Workspace 中可自主维护的持久化资源
中心，并能在有长期复用价值时编写 Tool、配置依赖与环境、沉淀 Skill 或 SOP、保存
知识和工作记忆。同时，提示词会区分模型管理的源内容与 Runtime 管理的派生状态，
并如实说明各类资源的生效时机。

## 变更

- 重组静态 system message，先定义 Workspace 和 `.workspace` 的统一心智模型，再
  介绍能力发现、Library、Execution、文件操作和工作方法。
- 明确模型可自主创建、整理、改进和删除 `.workspace` 中有复用价值的源内容，并列举
  Tool、依赖、环境、Skill、SOP、知识和工作记忆等沉淀方式。
- 禁止修改生成的 `index.md` 和 Runtime 内部状态，并说明 Tool、Skill、依赖和
  `.workspace/env` 在 Runtime 重新打开后生效，Library 源内容在活动 Runtime 中
  reconcile。
- 精简重复的 Shell 探索和内置工具说明，同时保留 `files write` / `files edit` 的
  精确语法、Library 状态语义、按需读取和并行观察边界。
- 更新 system message 契约测试，覆盖 `.workspace` 自主管理、自进化方式、持久化
  克制原则、Runtime 管理边界和资源生效时机。
