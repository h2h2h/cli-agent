# refactor(runtime): route Tools through the Custom command registry

**状态：** resolved

## 背景

当前 `tools list`、`tools info` 和 `tools run` 依赖 `command.tool` 与独立的
`_ToolDriver` 分支。这样 Tools 不符合 Custom-first routing 的统一规则，也无法
直接复用 Custom command 的 name、prepare、isolated 和 parallel-safe 合同。

## 影响

完成后，所有 Runtime-owned command 都由 `_CustomCommandRegistry` 统一管理：
`cd`、`export` 和 `tools` 通过同一条 Custom route 执行；未命中 Registry 的命令
才进入 Shell fallback。非法 Tools 命令仍由 `tools` handler 返回失败，不能落到
Host Shell。

## 变更

- 在默认 Custom registry 中注册命令名 `tools`。
- 将现有 Tool prepare 逻辑迁移为 `handlers/tools.py` 中的 Tools Custom command。
- 由 Tools handler 在 prepare 或 parallel-safe 判断阶段调用纯的 Tool grammar，
  不再修改 `CommandParseResult`。
- 将 Tool Catalog 和 Tool Environment 作为 Tools command 的构造依赖注入。
- 当 Tool Environment 不可用时：
  - `tools list` 继续返回 Catalog projection；
  - `tools info` 继续返回 Catalog facts；
  - `tools run` 返回普通 failed Execution，不回退到 Host Python。
- 修改 Registry 的匹配逻辑：
  - 第一个命令 token 精确匹配 Custom name；
  - tokenization 失败时仍能识别原始 `tools` command head；
  - `./tools`、`/bin/tools` 和 `toolsmith` 不匹配 `tools`；
  - 命中 Custom 后，即使包含 pipeline、redirection 或非法参数，也不得 Shell
    fallback。
- 规定 `cd`、`export`、`tools` 为 Runtime 保留 command name，重复注册默认失败，
  不允许普通 `register()` 静默覆盖。
- 删除 Router 的 Tool special branch 和 `_ToolDriver` 依赖。
- 增加 `tools list/info/run`、非法语法、malformed quote、pipeline、redirection
  和 Host executable collision 测试。
