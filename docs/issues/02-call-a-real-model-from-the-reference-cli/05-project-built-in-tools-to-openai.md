# feat(provider): project built-in tools into OpenAI requests / 将内置工具投影到 OpenAI 请求

**Status / 状态：** pass

## English

### Background

Every `ModelRequest` already carries the canonical `exec`, `output`, and `kill` definitions, but the OpenAI-compatible Adapter does not include them in its request payload. A real model therefore cannot discover or call the Runtime's built-in tools.

This projection is an Adapter concern: the provider-neutral `ToolSchema` must remain unchanged.

### Changes

- Convert every `ModelRequest.tools` entry into the supported Chat Completions function-tool shape.
- Send the canonical name, description, and input schema in their existing order.
- Do not send the Runtime result schema as a function parameter schema or add provider fields to `ToolSchema`.
- Keep tool selection on the Provider's normal automatic behavior for this minimal subset.
- Extend fake-transport request tests to assert exactly `exec`, `output`, and `kill`, with no dynamic capability schemas.

## 中文

### 背景

每个 `ModelRequest` 已经携带规范的 `exec`、`output` 和 `kill` 定义，但 OpenAI-compatible Adapter 尚未把它们加入请求 payload。因此真实模型无法发现或调用 Runtime 的内置工具。

该投影属于 Adapter；供应商中立的 `ToolSchema` 必须保持不变。

### 变更

- 将每个 `ModelRequest.tools` 条目转换为受支持的 Chat Completions function-tool 形状。
- 按现有顺序发送规范名称、描述和 input schema。
- 不把 Runtime result schema 当作 function 参数 schema 发送，也不向 `ToolSchema` 添加供应商字段。
- 在最小子集中继续使用 Provider 默认的自动工具选择行为。
- 扩展 fake transport 请求测试，断言只存在 `exec`、`output` 和 `kill`，且没有动态能力 schema。
