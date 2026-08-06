# refactor(backend): route Shell through Local Backend

**状态：** resolved

## 背景

`_ShellHandler` 当前直接组合 `os.environ`、Host cwd、
`asyncio.create_subprocess_shell()` 与 process-group 参数，并注入具体
`_CapabilityView` 完成 redirect copy-up。这使 Handler 同时拥有 Shell 命令适配、
Local 进程实现和 Local Capability View 协调。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，Shell Handler 只把已有 `ShellParseResult`、Backend cwd 与 Session 环境
转换为 `_ShellExecutionRequest`。Local subprocess、输出捕获、process group、
取消和 Local copy-up preparation 全部位于 Local Backend 内，Supervisor 无需改变。

## 变更

- 让 `_ShellHandler` 依赖 `_BackendWorkspace`，并调用
  `prepare_shell(request)`；删除 Handler 对 `asyncio`、`os`、Host `Path` 与具体
  Capability View 的依赖。
- `_ShellExecutionRequest.command` 直接携带现有 `ShellParseResult`，Backend 不
  重新 parse raw command。
- 在 Local Backend 中创建 `_LocalShellExecution` 或等价实现：
  - run 时才创建 subprocess；
  - Backend 组合 execution base environment 与 Session overlay；
  - 保留 stdout/stderr streaming 与 POSIX process-group cancellation；
  - Local View 暂时消费现有 redirect mutation facts，后续 issue 05 完成分层。
- 保持 queued-before-run cancellation 不创建进程，保持 failed/killed outcome 与
  output bound 不变。
- 增加 Handler request 测试、Local shell execution 测试和静态 import 回归。

## 验收标准

- [ ] `_ShellHandler` 不创建 Host subprocess、不读取 `os.environ`。
- [ ] Local Backend 是普通 Shell subprocess 的唯一 owner。
- [ ] Shell output、exit code、kill、close 与并行调度行为无回归。
- [ ] Capability redirect copy-up 仍发生在命令启动前。

