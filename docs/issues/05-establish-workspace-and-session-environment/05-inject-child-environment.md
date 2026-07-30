# feat(environment): inject Host and Session environment into Shell / 向 Shell 注入 Host 与 Session 环境

**Status / 状态：** pass

## English

### Background

The accepted AEP-style contract intentionally exposes the complete embedding
process environment while allowing Workspace and Session values to override
same-named entries.

### Changes

- Bind `child_env = dict(os.environ) | session.env` when a Shell Execution
  starts.
- Pass `env=child_env` explicitly to the subprocess API.
- Prove Session values override Host values and do not mutate `os.environ`.
- Prove a Host environment change affects later Executions without a Runtime
  reopen, while a `.workspace/env` file edit does not.
- Keep inherited Host and preexisting Session mappings out of Command Parse
  Result, policy facts, denial messages, and Runtime diagnostics. An explicit
  export assignment remains part of the exact structured operation.
- Document and test that Provider credentials in `os.environ` are available to
  Agent commands.
- Do not add filtering, controlled defaults, Host grants, value redaction, or
  environment generations.

## 中文

### 背景

已接受的 AEP-style 契约有意暴露完整 embedding process 环境，同时允许
Workspace 与 Session 值覆盖同名 entry。

### 变更

- Shell Execution 启动时绑定
  `child_env = dict(os.environ) | session.env`。
- 向 subprocess API 显式传递 `env=child_env`。
- 证明 Session 值覆盖 Host 值且不修改 `os.environ`。
- 证明 Host 环境修改无需 Runtime reopen 即影响后续 Execution，而
  `.workspace/env` 文件修改不会。
- 继承的 Host 与既有 Session mapping 不得进入 Command Parse Result、policy
  fact、denial message 或 Runtime diagnostic。显式 export assignment 仍属于
  精确 structured operation。
- 文档和测试明确 `os.environ` 中的 Provider credential 可被 Agent 命令访问。
- 不添加 filtering、controlled default、Host grant、value redaction 或
  environment generation。
