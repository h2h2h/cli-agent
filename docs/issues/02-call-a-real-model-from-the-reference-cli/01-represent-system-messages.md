# feat(model): represent provider-neutral System Messages / 表示供应商中立的 System Message

**Status / 状态：** pass

## English

### Background

The provider-neutral conversation currently represents User, Assistant, and Tool Result Messages, but it has no System Message. The OpenAI-compatible Adapter cannot cover the supported system role without placing a provider payload dictionary or an untyped string outside the model seam.

This issue adds only the missing conversation type. Runtime assembly and OpenAI wire conversion belong to later issues.

### Changes

- Add an immutable `SystemMessage` containing ordered text content and a `text` constructor consistent with the existing message types.
- Include `SystemMessage` in `ModelMessage` and `ModelRequest` history.
- Export `SystemMessage` through the public `cli_agent.runtime` package.
- Keep provider role names and wire dictionaries out of the type.
- Add model and public-surface tests covering construction, equality, history ordering, and importability.

## 中文

### 背景

供应商中立的 Conversation 当前可以表示 User、Assistant 和 Tool Result Message，但缺少 System Message。若没有这个类型，OpenAI-compatible Adapter 就只能把供应商 payload 字典或无类型字符串带到模型 seam 之外，才能支持 system role。

本 issue 只补充缺失的 Conversation 类型；Runtime 组装和 OpenAI wire 转换属于后续 issue。

### 变更

- 添加不可变的 `SystemMessage`，保存有序文本内容，并提供与现有 Message 一致的 `text` 构造方法。
- 将 `SystemMessage` 加入 `ModelMessage` 和 `ModelRequest` History。
- 通过公共 `cli_agent.runtime` package 导出 `SystemMessage`。
- 不在该类型中加入供应商 role 名称或 wire 字典。
- 添加模型与公共界面测试，覆盖构造、相等性、History 顺序和可导入性。
