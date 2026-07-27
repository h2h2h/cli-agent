# test(cli): prove cli-agent with a fake model transport / 使用 fake model transport 验证 cli-agent

**Status / 状态：** pass

## English

### Background

The parent issue is complete only when executable cli-agent proves the same real-Adapter Agent path available to any embedding host. Unit tests for parsing, rendering, and transport conversion do not by themselves prove that the pieces are wired together without command-line-only capabilities.

The final scenario must be fully offline and stable in the normal test suite.

### Changes

- Invoke the cli-agent entry path with a temporary Workspace and captured standard streams.
- Inject a deterministic fake HTTP transport that returns a fragmented short-command Tool Call followed by final model text and usage.
- Assert the first request begins with the Runtime-assembled System Message and User task, and carries exactly the three built-in tools.
- Assert the second request contains the matching Assistant Tool Call and Tool Result produced by the Runtime.
- Assert streamed text, Tool Call diagnostics, completion metadata, and successful exit status reach the CLI user.
- Assert Session and Runtime resources close after success.
- Fail the test on any external network attempt and require no live credential.
- Add a boundary assertion that the CLI imports only public Runtime and Provider APIs and owns no command-execution behavior.

## 中文

### 背景

只有当可执行 cli-agent 证明它使用了与所有嵌入式宿主相同的真实 Adapter Agent 路径时，父 issue 才算完成。解析、展示和 transport 转换的单元测试本身无法证明各部分已经正确连接，也无法证明命令行入口没有私有能力。

最终场景必须完全离线，并能稳定运行在普通测试套件中。

### 变更

- 使用临时 Workspace 和捕获的标准流调用 cli-agent entry path。
- 注入确定性的 fake HTTP transport，先返回分片短命令 Tool Call，再返回最终模型文本和 usage。
- 断言第一次请求以 Runtime 组装的 System Message 和 User task 开始，并且只携带三个内置工具。
- 断言第二次请求包含匹配的 Assistant Tool Call 和由 Runtime 产生的 Tool Result。
- 断言流式文本、Tool Call 诊断、completion metadata 和成功退出状态到达 CLI 用户。
- 断言成功后 Session 与 Runtime 资源均被关闭。
- 任何外部网络尝试都让测试失败，且不需要真实凭据。
- 添加边界断言，确保 CLI 只导入公共 Runtime 和 Provider API，并且不持有命令执行行为。
