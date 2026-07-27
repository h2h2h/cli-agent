# test(runtime): prove the smallest deterministic Agent Loop / 验证最小确定性 Agent Loop

**Status / 状态：** pass

## English

### Background

The preceding changes establish the individual modules needed by issue 01. The parent issue is complete only when one offline scenario proves the entire path through the public host interface rather than by constructing private modules in the test.

This test is the executable contract for the first usable Runtime and should remain stable while later issues deepen execution, capability, and provider behavior.

### Changes

- Add an end-to-end test that opens `AgentRuntime`, gets or creates a Session, submits a user turn, and consumes the resulting events.
- Configure `ScriptedModelProvider` to request a short `exec` command on its first model turn and return final assistant text after receiving the Tool Result.
- Assert that every captured `ModelRequest` exposes exactly `exec`, `output`, and `kill`.
- Assert that Conversation History contains the user message, assistant Tool Call, matching Tool Result, and final assistant message in order.
- Include a scripted turn with multiple Tool Calls and assert serial model-returned dispatch order.
- Assert the command output reaches the scripted model and the expected final text reaches the host.
- Close the Session and Runtime through the public lifecycle and assert their owned state is released.
- Prevent all network access in the scenario.
- Remove superseded private-module tests only where the same behavior is now covered more durably through the public Runtime interface.

## 中文

### 背景

前序变更已经建立 issue 01 所需的各个模块。只有当一个离线场景通过公共宿主 interface 验证完整路径，而不是在测试中直接构造私有模块时，父 issue 才算完成。

该测试是第一个可用 Runtime 的可执行契约。后续 issue 加深 Execution、能力和 Provider 行为时，它应保持稳定。

### 变更

- 添加端到端测试：打开 `AgentRuntime`、获取或创建 Session、提交用户 turn，并消费产生的事件。
- 配置 `ScriptedModelProvider`，使其在第一次模型 turn 中请求一个短 `exec` 命令，并在收到 Tool Result 后返回最终助手文本。
- 断言捕获到的每个 `ModelRequest` 都只暴露 `exec`、`output` 和 `kill`。
- 断言 Conversation History 按顺序包含用户消息、助手 Tool Call、匹配的 Tool Result 和最终助手消息。
- 加入包含多个 Tool Call 的脚本 turn，并断言它们按模型返回顺序串行派发。
- 断言命令输出到达脚本模型，预期最终文本到达宿主。
- 通过公共生命周期关闭 Session 和 Runtime，并断言其自有状态已释放。
- 阻止该场景中的所有网络访问。
- 仅在相同行为已经通过公共 Runtime interface 获得更稳定覆盖时，移除被取代的私有模块测试。
