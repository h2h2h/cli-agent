# feat(model): define the fixed built-in tools / 定义固定的内置工具

**Status / 状态：** pass

## English

### Background

The parent issue requires every model request to expose exactly `exec`, `output`, and `kill`. The current `ModelRequest` contains only messages, so callers could neither describe the environment interface nor verify that it remains independent of Runtime capabilities.

The three built-in tools form a fixed protocol, not a dynamic Tool registry. Later Skills, Tools, Library content, and MCP projections must not add model-visible schemas.

### Changes

- Define immutable, provider-neutral schemas for `exec`, `output`, and `kill` using the architecture-approved names, descriptions, parameters, and result contract.
- Add the fixed built-in tool schema tuple to `ModelRequest`.
- Provide one canonical construction path so the Agent Loop cannot assemble a different schema set per request.
- Keep capability configuration and discovered capability names out of the schema types.
- Add contract tests asserting the exact schema count, names, order, and JSON-serializable shape.
- Add a test proving that unrelated Runtime capability metadata cannot alter the built-in tool tuple.

## 中文

### 背景

父 issue 要求每个模型请求都只暴露 `exec`、`output` 和 `kill`。当前 `ModelRequest` 只包含消息，因此调用方既无法描述环境界面，也无法验证该界面是否始终独立于 Runtime 能力。

三个内置工具构成固定协议，而不是动态 Tool registry。后续加入的 Skill、Tool、Library 内容和 MCP 投影都不得增加模型可见 schema。

### 变更

- 使用架构批准的名称、描述、参数和结果契约，为 `exec`、`output`、`kill` 定义不可变、供应商中立的 schema。
- 将固定的内置工具 schema 元组加入 `ModelRequest`。
- 提供唯一的规范构造路径，防止 Agent Loop 为不同请求组装不同的 schema 集合。
- 将能力配置和发现到的能力名称排除在 schema 类型之外。
- 添加契约测试，断言准确的 schema 数量、名称、顺序及 JSON 可序列化形状。
- 添加测试，证明无关的 Runtime 能力元数据无法改变内置工具元组。
