# feat(prompt): register files command and guide model file mutations

**状态：** resolved

## 背景

RFC-0009 的 `files write` / `files edit` handler 实现完成后（issue 01-04），
还需要注册 custom command 并让模型采纳：模型目前只会用 Shell 写文件，而
`_DIRECT_MUTATORS` 无法穷举这些写法。注册与提示词指引必须一起落地，命令
才真正可用。

## 影响

完成后，`files write` / `files edit` 进入与 `tools` 相同的 Custom-first
routing；system message 明确要求模型用 `files` 命令完成文件修改，禁止
`tee`、`sed -i`、`cat >`、`echo >`、heredoc 重定向、Python 脚本等 Shell
写法。模型变更行为的收敛是 issue 07 删除 `_DIRECT_MUTATORS` 与 Shell 变更
启发式的前提。

## 变更

- 在 `kernel.py` 与 `tool_command` 并列注册
  `_CustomCommand(name="files", prepare=_FileHandler(view).prepare,
  parallel_safe=False, isolated=True)`；`_FileHandler` 构造时注入
  `capability_view`（与 `_ShellHandler` 相同依赖）。
- 路由与调度约束：
  - `parallel_safe=False`：`files` 命令不得进入并行 batch；
  - 非法 `files` 语法返回 failed execution，不落 Shell fallback；
  - 与 `tools` 相同，注册后在 Kernel 内唯一，不允许静默覆盖。
- 更新 `_system_message.py` 的 "Workspace file operations" 段的 Write 部分：
  - 新建/覆盖文件使用 `files write <path> <<'EOF' ... EOF`；精确修改使用
    `files edit <path> <<'EDI' {...} EDI`，一次调用可含多个 edit；
  - 不要使用 `tee`、`sed -i`、`cat >`、`echo >`、heredoc 重定向或 Python
    脚本等方式写文件；`files` 命令负责处理 Capability View；
  - 保留既有"写前读目标、写后 `git diff` 验证"规则。
- 更新 `test_system_message.py`：断言指引段包含 `files write` / `files edit`
  用法与禁止 Shell 写文件条目。
- 更新 README 的 Built-in commands 说明与 `docs/architecture.md` 的 Handler
  边界（如适用）。

## 验收标准

- [ ] `files` 注册成功，非法用法命中 Custom route 且不落 Shell。
- [ ] `files` 命令 `parallel_safe=False`，与 `tools` 一样不可被覆盖注册。
- [ ] system message 指引段包含 `files write` / `files edit` 用法与禁止
      Shell 写文件的条目。
- [ ] 相关测试断言与文档同步更新。
