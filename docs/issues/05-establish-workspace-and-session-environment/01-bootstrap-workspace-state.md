# feat(workspace): bootstrap `.workspace` at Runtime open / Runtime open 时建立 `.workspace`

**Status / 状态：** pass

## English

### Background

Runtime open needs one persistent bootstrap phase before it loads Workspace
configuration, performs future reconciliation, or creates Sessions.
`.workspace` is a reserved state namespace, not the Workspace root or
Capability View.

### Changes

- Add a private Workspace-open helper invoked by `AgentRuntime.open` before
  Environment Session creation.
- Create the `.workspace` directory and empty `.workspace/env` regular dotenv
  file idempotently when absent, using restrictive creation permissions where
  the platform supports them and respecting the Host umask.
- Reuse a valid existing directory and regular file without changing their
  permissions or contents.
- Reject symbolic links and objects of the wrong type at either required path
  with a stable Workspace-open failure.
- Make concurrent create attempts race-safe by validating the resulting object
  after idempotent creation.
- Leave the persistent namespace in place after Runtime close or a later open
  failure; never delete or roll back user data.
- Do not create future capability, Tool Environment, generated, whiteout, or
  lock subtrees in this issue.

## 中文

### 背景

Runtime open 在加载 Workspace 配置、执行未来 reconciliation 或创建 Session
前，需要统一的持久 bootstrap 阶段。`.workspace` 是保留状态 namespace，不是
Workspace root 或 Capability View。

### 变更

- 添加由 `AgentRuntime.open` 在 Environment Session 创建前调用的私有
  Workspace-open helper。
- 缺失时幂等创建 `.workspace` 目录和空的 `.workspace/env` 普通 dotenv
  文件；平台支持时请求收紧权限，同时尊重 Host umask。
- 复用合法已有目录和普通文件，不修改权限或内容。
- 任一必需路径为符号链接或错误对象类型时，以稳定 Workspace-open failure
  拒绝。
- 幂等创建后重新验证实际对象，使并发创建竞态安全。
- Runtime close 或之后的 open 失败均保留 namespace；不得删除或回滚用户数据。
- 本 issue 不创建未来 capability、Tool Environment、generated、whiteout 或
  lock 子树。
