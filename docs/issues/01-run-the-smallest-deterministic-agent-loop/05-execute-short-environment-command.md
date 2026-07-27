# feat(environment): execute a short command through built-in tools / 通过内置工具执行短命令

**Status / 状态：** pass

## English

### Background

There is currently no Environment Kernel or Session-scoped Environment Binding. The Agent Loop therefore has nowhere to dispatch a model Tool Call. Issue 01 needs only enough environment behavior to execute a short command deterministically while establishing the internal seam used by all three built-in tools.

Long-running Executions, FIFO queues, bounded cursor buffers, process-group cancellation, and cross-Session isolation belong to later implementation issues and must not be pulled into this change.

### Changes

- Add a private `EnvironmentKernel` module that owns minimal environment-session state.
- Add a private `EnvironmentBinding` that routes `exec`, `output`, and `kill` to one environment session without exposing `session_id` to the model.
- Execute a short local command with the Workspace as its default working directory and capture ordered stdout, stderr, exit status, and an opaque execution ID.
- Return provider-neutral, JSON-serializable Tool Results using the architecture-approved execution result shape.
- Support `output` for retained completed-command output and make `kill` of an already completed execution an idempotent result.
- Reject Tool Calls outside the three built-in tools and return structured argument or execution errors as Tool Results.
- Close the environment session and kernel idempotently.
- Add tests through the Environment Binding for successful output, non-zero exit, invalid arguments, unknown execution IDs, and cleanup.

## 中文

### 背景

项目当前没有 Environment Kernel，也没有 Session 作用域的 Environment Binding，因此 Agent Loop 无处派发模型 Tool Call。Issue 01 只需要足够的环境行为来确定性执行短命令，同时建立三个内置工具共用的内部 seam。

长时间运行的 Execution、FIFO 队列、有界 cursor buffer、进程组取消和跨 Session 隔离属于后续实现 issue，不应进入本次变更。

### 变更

- 添加私有 `EnvironmentKernel` 模块，持有最小环境 Session 状态。
- 添加私有 `EnvironmentBinding`，将 `exec`、`output`、`kill` 路由到一个环境 Session，且不向模型暴露 `session_id`。
- 以 Workspace 为默认工作目录执行本地短命令，并捕获有序 stdout、stderr、退出状态和不透明 execution ID。
- 使用架构批准的执行结果形状，返回供应商中立且可 JSON 序列化的 Tool Result。
- 支持读取已完成命令保留输出的 `output`，并让针对已完成 Execution 的 `kill` 返回幂等结果。
- 拒绝三个内置工具之外的 Tool Call，并将结构化参数错误或 Execution 错误作为 Tool Result 返回。
- 以幂等方式关闭环境 Session 和 Kernel。
- 添加经过 Environment Binding 的测试，覆盖成功输出、非零退出、无效参数、未知 execution ID 和资源清理。
