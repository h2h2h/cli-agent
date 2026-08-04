---
rfc_id: RFC-0009
title: File Write and Edit Commands for Trusted File Mutations
status: PROPOSED
author: cli-agent maintainers
reviewers:
  - name: project owner
    status: pending
created: 2026-08-03
last_updated: 2026-08-03
related_prds: []
related_rfcs:
  - RFC-0001-host-mediated-execution-approval.md
  - RFC-0002-workspace-capability-view.md
  - RFC-0003-tool-capability-commands.md
  - RFC-0008-shell-ast-pluggable-policy-and-guided-exploration.md
---

# RFC-0009: File Write and Edit Commands for Trusted File Mutations

## 概述

本 RFC 为 cli-agent 增加 `files write` 和 `files edit` 两个 Runtime-owned 命令，
作为文件修改的首选通道。`files write <path> <<'EOF' ... EOF` 新建或覆盖文件；
`files edit <path> <<'EDI' {...} EDI` 对单个文件做一次或多次精确文本替换。
两个命令都通过 Custom command registry 路由到 Runtime 内实现，不经过 Host
Shell，也不新增模型可见 syscall（模型仍只使用 `exec`、`output` 和 `kill`）。

system message 相应更新：指导模型用这两个命令完成文件修改，而不是用
`tee`、`sed -i`、`cat >`、`echo >` 或 Python 脚本写文件。`_CapabilityView` 中
基于 `_DIRECT_MUTATORS` 的 Shell 变更启发式（`view.py:_may_mutate`）随之
**直接删除**，不保留兜底：`prepare_shell` 简化为仅基于 AST 输出重定向事实
做 copy-up，in-view 与否的判定统一收敛到按路径的 `prepare_path`。

## 背景与上下文

### 起点与已验证方向

当前 `_CapabilityView`（`runtime/_capability/view.py`）需要在下层 symlink
被覆盖前完成 copy-up、在删除后补齐 whiteout。为此它维护一份"直接文件变更命令"
清单 `_DIRECT_MUTATORS`（15 个名字）以及单独的 in-place `sed` 检测器
`_sed_is_in_place`，用于推断一条 Shell 命令是否可能修改 capability 目录
（`.workspace/tools`、`.workspace/skills`、`.workspace/library`、
`.workspace/_mcp`）内的文件（`view.py:27-45`、`view.py:48-51`、
`view.py:526-533`）。

该清单是启发式且不完整：模型完全可以用 `python -c "open(...)"`、
`git apply`、`perl -pi` 等不在清单内的方式写文件。清单膨胀的风险在于每一类
新写法都要同步进 `_write_paths` / `_delete_paths` 的 target 推断规则，而语法上
无法穷举。本 RFC 决定**删除**该清单与相关启发式：模型修改文件只通过
`files` 命令（system message 指引），Shell 变更不再有清单式保护。

### 已验证可行的既有模式

- Custom command 注册与分发已成熟：`tools` 在 `kernel.py:86-98` 注册为
  `_CustomCommand`，handler 按子命令分发（`tools list/info/run`），非法语法
  返回 failed execution 而不落 Shell。
- Shell AST 已覆盖 heredoc：`command_parser.py` 的 `HereDocRedirect` 直接携带
  `delimiter`、`body`（精确原文）、`expands`、`strip_tabs` 等事实；
  `tools/grammar.py` 已改用 `command.command_head` + `match root.argv,
  root.redirects` 的结构化模式匹配解析 `tools run <<'PY' ... PY`，不再
  用正则处理 `raw_command`。
- 参考实现（pi-agent）的 `write`/`edit` 工具（`packages/coding-agent/src/core/
  tools/write.ts`、`edit.ts`、`edit-diff.ts`）：edit 的核心语义是"每个 oldText
  在原始内容上精确匹配、校验唯一且互不重叠、按倒序应用"，并处理 BOM、行尾
  归一化与恢复。该语义可直接精简移植。

### 术语

| 术语 | 定义 |
|---|---|
| 文件修改命令 | `files write` 与 `files edit` 两个命令的合称 |
| payload | heredoc 体内的原文载荷（write 为文件内容，edit 为 JSON 文档） |
| in-view 路径 | 位于 `.workspace/{tools,skills,library,_mcp}` 下的有效路径 |
| lower link | Capability View 中指向 Repertoire 下层的 symlink |

## 问题陈述

模型修改文件目前只有一条路：通过 Shell 命令。这带来两个问题：

1. **Capability View 的变更检测不完备**。`_DIRECT_MUTATORS` 是静态清单，
   无法覆盖所有写文件方式；漏检意味着对 lower link 的写会直接穿透到
   Repertoire，破坏用户维护的内容。
2. **变更不可结构化**。Policy 只能看到 `ShellParseResult` 的语法事实，无法
   区分"读文件"与"精确替换某一段文本"；Host 无法基于"修改了什么"做细粒度
   授权；执行结果也只有 stdout/stderr，没有结构化反馈。

### 不作为的代价

- 清单继续膨胀，每次新增写文件方式都要维护 target 推断规则；
- 穿透写 Repertoire 的风险持续存在；
- 模型为规避引号问题写出越来越复杂的 Shell 拼接，可读性和可控性下降。

## 目标与非目标

### 目标

1. 提供 `files write` 与 `files edit` 两个命令，语法与既有 `tools` 命令一致。
2. 两个命令都路由到 Runtime 内实现，非法语法不得落 Shell fallback。
3. edit 对单个文件支持一次调用内多处替换，语义为"相对原始内容匹配、互不
   重叠、倒序应用"。
4. 对 in-view 路径，在写入前完成 Capability View copy-up，与 Shell 变更路径
   行为一致。
5. system message 明确指导模型使用这两个命令完成文件修改。
6. 删除 `_DIRECT_MUTATORS`、`_sed_is_in_place` 及 Shell target 推断规则，
   `prepare_shell` 简化为仅基于 AST 输出重定向事实。
7. 不新增模型可见 syscall；不引入 diff/patch 展示等非必要能力。

### 非目标

1. 提供 Shell 语义之外的合并、冲突解决、fuzzy 匹配或 diff 输出。
2. 将文件修改做成模型可见 syscall，或新增第二个 execution 通道。
3. 对 workspace 之外的路径做强制限制（workspace 是组织边界，不是安全边界，
   与现有 `cd`/Shell 语义一致）。
4. 引入并行授权：`files` 命令一律 `parallel_safe=False`。
5. 为 Shell 变更保留清单式保护（删除后 Shell 对 capability 目录的变更不再
   有启发式兜底，仅输出重定向仍自动 copy-up）。

### 成功标准

- [ ] `files write <path> <<'EOF' ... EOF` 可新建、覆盖文件并自动创建父目录。
- [ ] `files edit` 支持单次调用多处替换，且每个 oldText 必须唯一、非空、
      互不重叠，否则返回带原因的 failed execution。
- [ ] `files` 命令的非法用法（缺子命令、缺 heredoc、未知子命令）返回 usage
      错误，不落 Shell。
- [ ] 对 `.workspace/tools` 等 in-view 路径写入时触发 copy-up，Repertoire
      下层文件不被修改。
- [ ] system message 的 file-read 指引段包含 `files write` / `files edit` 用法，
       并禁止用 Shell 手段写文件。
- [ ] `_DIRECT_MUTATORS`、`_sed_is_in_place`、`_operands` 与
       `_write_paths`/`_delete_paths` 的 executable 分支已删除，`prepare_shell`
       只处理 AST 输出重定向的 copy-up。
- [ ] 既有测试与新增测试全部通过，lint 无告警。

## 评估标准

| 标准 | 权重 | 描述 | 最低阈值 |
|---|---|---:|---|
| 与既有命令模式一致 | 高 | 复用 Custom registry、heredoc 载荷、Inline execution | 无新执行通道 |
| 内容传递健壮性 | 高 | 模型生成的内容不被 Shell 引号破坏 | 多行/特殊字符内容完整传递 |
| 语义确定性 | 高 | 匹配失败有明确、可操作的错误 | 每个失败分支有独立错误信息 |
| Capability View 正确性 | 高 | 不破坏 Repertoire 下层文件 | in-view 写入先 copy-up |
| 模型采纳成本 | 中 | system message 增量小、语法易学 | 两条固定语法 |
| 实施成本 | 中 | 改动面小、无新依赖 | 不引入外部 diff/匹配库 |

## 选项分析

### 选项 1：单一 `files` 命令，heredoc 承载载荷

**描述**

注册一个 head 为 `files` 的 Custom command（与 `tools` 同模式），handler 按
AST 子命令分发 `files write` / `files edit`：

```text
files write <path> <<'EOF'
<content>
EOF

files edit <path> <<'EDI'
{"edits": [{"oldText": "...", "newText": "..."}, ...]}
EDI
```

**优点**

- 与 `tools` 的"单 head 多子命令"结构完全一致，routing、policy、测试模式全部
  复用。
- heredoc 载荷由 AST `HereDocRedirect.body` 直接给出，模型生成的内容不受引号
  转义影响。
- `files` 是复数形式，不遮蔽系统 `file` 命令（文件类型检测），也保留单 head
  名字空间。
- `files` 名字空间可容纳未来的 `files copy`、`files move` 等子命令。

**缺点**

- heredoc 终止符若与内容首行冲突会误截断。write 内容中出现独立一行的 `EOF`
  时需要拆分写入；edit 使用固定标记 `EDI`，payload 是 JSON，冲突概率低。
- 单 head 意味着非法子命令（如 `files nonsense`）也要在 handler 内拒绝，不能
  靠 registry 头匹配完成。

**评估**

| 标准 | 评分 | 说明 |
|---|---|---|
| 与既有命令模式一致 | 好 | 与 `tools` 完全同构 |
| 内容传递健壮性 | 好 | heredoc 原文提取 |
| 语义确定性 | 好 | 固定语法，错误集中在 handler |
| Capability View 正确性 | 好 | handler 内统一 copy-up |
| 模型采纳成本 | 好 | 两条固定语法，system message 一行说明 |
| 实施成本 | 好 | 一个 handler、一个注册点 |

**工作量**：S-M。**风险**：heredoc 终止符冲突（`EOF` 固定标记）。

### 选项 2：独立的 `write` 与 `edit` 命令

**描述**

注册两个 head：`write <path> <<'EOF' ... EOF` 与
`edit <path> <<'EDI' {...} EDI`，各自独立的 handler。

**优点**

- 不引入"子命令"概念，注册即分发，`_CustomCommandRegistry.resolve` 直接完成
  匹配。
- `write`/`edit` 在 macOS 上是已废弃的通信/邮件工具，实际冲突可忽略。

**缺点**

- 与 `tools list/info/run` 的既有模式不一致，Handler 命名与注册点增多。
- 命令名字面过于通用，与作业系统 `write`（终端通信）、历史 `edit` 混淆风险
  高于带名字空间的 `files`。
- 未来新增 `files copy` 等能力时名字空间无从生长。

**评估**

| 标准 | 评分 | 说明 |
|---|---|---|
| 与既有命令模式一致 | 中 | 无子命令先例 |
| 内容传递健壮性 | 好 | heredoc 同上 |
| 语义确定性 | 好 | 同上 |
| Capability View 正确性 | 好 | 各自 handler 内 copy-up |
| 模型采纳成本 | 好 | 两个平级命令 |
| 实施成本 | 中 | 注册点与 handler 各二 |

**工作量**：S。**风险**：命名空间不可扩展，与既有模式不一致。

### 选项 3：新增模型可见 syscall

**描述**

在 `_syscalls.py` 的 `BUILT_IN_SYSCALL_SCHEMAS` 中新增 `file_write` /
`file_edit`，参数为结构化 JSON（`path`、`content` / `edits`），Kernel 直接
dispatch，不经命令解析。

**优点**

- 参数天然结构化，无 heredoc 解析，pi-agent 的 schema 可几乎原样移植。
- 匹配失败等错误可由 schema 校验提前拦截。

**缺点**

- 与 RFC-0008 及 issue 16-07 固定下来的"model-visible syscall 只有 `exec`、
  `output`、`kill`"契约冲突，需要改动 Provider 投影与全部 syscall 契约测试。
- 内容传输与执行生命周期需要第二条通道，Scheduler/输出快照逻辑无法复用
  （或必须将 syscall 伪装成命令）。
- 模型在 batch 中已有的工具选择模型（`exec`）上需要额外的 schema 投影和
  系统提示词说明，采纳成本反而更高。

**评估**

| 标准 | 评分 | 说明 |
|---|---|---|
| 与既有命令模式一致 | 差 | 破坏固定 syscall 契约 |
| 内容传递健壮性 | 好 | 结构化参数 |
| 语义确定性 | 好 | schema 校验 |
| Capability View 正确性 | 中 | 需为 syscall 另建 copy-up 挂钩 |
| 模型采纳成本 | 中 | 新工具 schema + 新提示词段落 |
| 实施成本 | 高 | Provider、协议、契约测试全链路改动 |

**工作量**：L。**风险**：与既有 syscall 边界架构冲突。

### 选项对比总结

| 标准 | `files` 单命令 | 独立命令 | 新 syscall |
|---|---|---|---|
| 与既有模式一致 | 好 | 中 | 差 |
| 内容传递 | 好 | 好 | 好 |
| 语义确定性 | 好 | 好 | 好 |
| View 正确性 | 好 | 好 | 中 |
| 模型采纳成本 | 好 | 好 | 中 |
| 实施成本 | 好 | 中 | 差 |

## 推荐

采用选项 1：单一 `files` 命令，heredoc 承载载荷。它与 RFC-0003/0007 确立的
`tools` 命令模式同构，实施面最小，且保留未来扩展子命令的余地。

接受以下取舍：

1. 命令 head 采用复数 `files`，避开系统 `file` 命令（类型检测），同时与
   `tools` 的单 head 子命令结构保持一致。
- heredoc 终止符固定为 `EOF` / `EDI`：tree-sitter 与 bash 一致，按第一个
  匹配行终止内容；write 内容需要写入字面 `EOF` 行时需换标记或拆分写入。
3. `files` 一律不并行：与 `cd`/`export` 相同的保守调度事实。

## 技术设计

### 命令语法

命令头匹配 `command.command_head`（AST 静态 head，不允许 prefix
assignments）。载荷解析沿用 `tools/grammar.py` 的结构化模式匹配，不用正则
处理 `raw_command`。固定标记：

```text
files write <path> <<'EOF'
<content>
EOF

files edit <path> <<'EDI'
{"edits": [{"oldText": "...", "newText": "..."}, ...]}
EDI
```

- 语法约束为精确 `SimpleCommand` 形态：argv 恰为 `(子命令, <path>)` 两元组、
  redirects 恰为单个 `HereDocRedirect`；pipeline、`&&`、多重重定向、prefix
  assignment 一律不匹配，返回 usage 错误。
- path 取 `ShellWord.value`（引号已剥、动态展开词如 `$VAR` 为 `None` 即拒绝）；
  delimiter 取 `HereDocRedirect.delimiter.value == "EOF"`，`'EOF'` / `"EOF"` /
  `EOF` 三种写法等价；`heredoc.operator` 必须恰为 `<<`，`<<-` 被拒。
- write 的 content 为 `heredoc.body.text` 原文（含终止符前的换行，与 bash
  heredoc 语义一致），不做变量展开或转义处理。
- edit 的 payload 为 UTF-8 JSON，用 `json.loads` 解析；`edits` 必须是非空数组，
  每项含非空 `oldText` 与 `newText`。
- 支持引号形式 `files edit <path> '<json>'` 作为单行便捷写法（与
  `tools run "..."` 对应）。

### 路由与调度

- 在 `kernel.py` 与 `tool_command` 并列注册
  `_CustomCommand(name="files", prepare=_FileHandler(view).prepare, parallel_safe=False, isolated=True)`。
- `parallel_safe=False`：写操作不允许进入并行 batch。
- 与 `tools` 相同，非法语法、未知子命令、heredoc 缺失一律返回 failed
  execution，不落 Shell fallback。
- 注册放在 kernel 而非 `_builtin_custom_commands()`，因为 handler 依赖注入的
  `_CapabilityView`（`_ShellHandler` 已接受同一依赖）。

### Handler（handlers/files.py）

`_FileHandler.prepare(command, context)` 用与 `tools/grammar.py` 同构的模式
匹配解析（`command.command_head` + `match root.argv, root.redirects`），分发
write / edit，返回 `_InlineExecution`：

```python
case (
    (ShellWord(value="write"), ShellWord(value=path)),
    (HereDocRedirect() as heredoc,),
) if (
    path is not None
    and heredoc.operator == "<<"
    and heredoc.delimiter.value == "EOF"
):
    return _write_facts(path, heredoc.body.text)
```

模块组织：`FileEdit`/`FileCommand` facts、`parse_files_command` 纯函数与
`_FileHandler` 同放 `handlers/files.py` 单模块——`files` 没有 catalog/
discovery/worker 等域内共享，facts 只被自身 handler 消费，不设独立能力域
包。`_capability/` 下的包（`tools`、`skills`、`mcp`）都是 Repertoire 驱动的
能力域（catalog + environment），`files` 只是 Runtime-owned 命令，与
`cd.py`/`export.py`/`tools.py` 的"命令家族一个文件"模式一致。

- **write**：提取 path 与 content → `mkdir(parents=True, exist_ok=True)` →
  写入 → stdout 输出 `wrote <n> bytes to <path>`。
- **edit**：读取文件 → 依次执行下述算法 → 写回 → stdout 输出替换处数与路径。

路径解析：`Path(path)`，相对路径以 `context.cwd` 为基准（与 `cd` 的
`_target_path` 一致），不做 workspace 强制。

### edit 匹配算法

1. 读文件字节，按 UTF-8 解码；失败则报错退出。
2. strip BOM 并记录，结束时重新前置。
3. 检测首个换行为 `\r\n` 或 `\n`，内容归一化为 LF。
4. 对每个 edit：`oldText` 在**归一化后的原始内容**上精确匹配
   （`str.find`，LF 归一化后的 oldText 同样归一化），要求恰好出现一次；
   空 oldText、未找到、多次出现分别返回独立错误。
5. 按匹配位置升序排序并检查互不重叠；重叠则报错并提示合并为单个 edit。
6. 按匹配位置**倒序**应用替换，保证偏移稳定。
7. 恢复行尾与 BOM，原子写回（临时文件 + `os.replace`）。

匹配语义与 pi-agent 一致：所有 oldText 相对原始内容匹配，而非增量匹配；
v1 不做 fuzzy 归一化匹配，避免行为不确定。

### Capability View 集成

`_CapabilityView` 新增 `prepare_path(path: Path)`：

- 目标不在 view 内时无操作；
- 目标是 lower link 时执行既有 `_copy_up`（转为 view 层实体文件），
  与 `prepare_shell` 的写前处理一致；
- 路径存在 whiteout 时移除 whiteout（写入即取消隐藏）；
- 中间目录不得穿越 symlink（复用 `_reject_symlink_intermediates`）。

`_FileHandler` 持有注入的 `_CapabilityView`，write/edit 写回前对解析后的
绝对路径调用 `prepare_path`。这样 capability 目录内的文件修改与 Shell 路径
行为一致，且**不需要** `_DIRECT_MUTATORS` 判定：in-view 与否由路径决定。

#### 删除 Shell 变更启发式

`_DIRECT_MUTATORS`、`_sed_is_in_place`、`_operands` 与
`_write_paths`/`_delete_paths` 的 executable 分支一并删除：
`_may_mutate` 简化为仅判断 `contains_output_redirection`，`_write_paths` 只
保留 AST redirect targets；`_snapshot_deletes`、`_reconcile_deletes`、
`_DeleteSnapshot`、`_create_whiteout` 随之删除（Shell 不再产生 whiteout），
保留 `_normalize_targets`、`_copy_up`、`_reject_symlink_intermediates` 与
`_remove_whiteout`（`_copy_up` 与 `prepare_path` 仍使用）。

行为变化（测试中固定）：

- Shell 命令对 capability 目录的变更仅剩输出重定向自动 copy-up，不再有
  清单式保护；模型按 system message 指引只通过 `files` 命令修改文件。
- Shell 删除（`rm`/`mv`）删除的是 view 层 symlink 本身，不穿透 Repertoire，
  但不再创建 whiteout：view 层删除不跨 attach 持久（下次 Runtime open 时
  Repertoire 文件会重新出现）。

### system message

在 `_system_message.py` 的 "Workspace file operations" 段的 Write 部分补充：

- 新建/覆盖文件使用 `files write <path> <<'EOF' ... EOF`；精确修改使用
  `files edit <path> <<'EDI' {...} EDI`，一次调用可含多个 edit。
- 不要使用 `tee`、`sed -i`、`cat >`、`echo >`、heredoc 重定向或
  Python 脚本等方式写文件；`files` 命令负责处理 Capability View。
- 写前读目标与上下文，写后 `git diff` 验证（保留现有规则）。

### 默认 Policy

当前代码没有内置默认 Policy（`ExecutionPolicy` 完全由 Host 注入）。`files`
命令照常经过 policy.evaluate；Host 可基于 `executable_basename == "files"`
实现细粒度授权。本 RFC 不新增 Policy 规则。

## 安全考量

| 威胁 | 影响 | 可能性 | 缓解 |
|---|---|---|---|
| 恶意 payload 写入 Repertoire 下层 | 高 | 低 | in-view 写入强制 copy-up；`prepare_path` 拒绝 symlink 中间目录 |
| heredoc 载荷被误解析 | 中 | 低 | tree-sitter AST 解析，终止符与 bash 一致按首个匹配行终止；`<<-`、多重重定向、非 `SimpleCommand` 形态一律拒绝 |
| `files` 命令被 Host executable 冲突 | 中 | 低 | Custom registry 优先于 Shell fallback，与 `tools` 同策略 |
| edit 匹配导致意外覆盖 | 高 | 低 | 唯一性 + 不重叠校验 + 倒序应用，错误信息可操作 |
| 模型绕过 `files` 直接用 Shell 写文件 | 中 | 中 | system message 指引；Shell 对 capability 目录的变更不再有清单式保护，仅 AST 输出重定向自动 copy-up |

`files` 是结构化的运行时行为，不是安全认证；workspace 不是操作系统安全边界。

## 实施计划

### 阶段 1：命令与 handler

- `handlers/files.py`：单模块命令家族——`FileEdit`/`FileCommand` facts、
  `parse_files_command` 纯函数解析、`_FileHandler`、write/edit 实现与全部
  错误分支。不新增 `_capability/files/` 能力域。
- `kernel.py`：注册 `files` custom command。
- 单元测试：payload 解析、write 新建/覆盖/父目录、edit 多处替换与倒序应用、
  not found/重复/空/重叠错误、引号形式。

### 阶段 2：Capability View 集成

- `view.py`：新增 `prepare_path`，复用 `_copy_up` / whiteout 逻辑。
- handler 写回前调用 `prepare_path`。
- 测试：in-view lower link 写入后 view 层为实体文件且 Repertoire 未变、
  whiteout 移除、symlink 中间目录拒绝。

### 阶段 3：system message 与回归

- `_system_message.py` 更新指引段；`test_system_message.py` 补充断言。
- 更新 README、相关 issue 记录。
- 运行完整测试与 lint（`ruff`、`mypy`）。

### 阶段 4：删除 Shell 变更启发式

- 删除 `_DIRECT_MUTATORS`、`_sed_is_in_place`、`_operands`、
  `_DeleteSnapshot`、`_delete_paths`、`_snapshot_deletes`、
  `_reconcile_deletes`、`_create_whiteout` 及不再使用的 import。
- `_may_mutate` 简化为仅 `contains_output_redirection`；`_write_paths` 仅保留
  redirect targets。
- 测试：重定向 copy-up 不回归；Shell `rm`/`mv` 不再产生 whiteout 的行为
  变化固定下来；静态回归确认生产代码不再出现上述 symbol。

### 回滚策略

从 `kernel.py` 移除 `files` 注册即恢复原状；`prepare_path` 为纯新增方法。
删除 `_DIRECT_MUTATORS` 等启发式属于单向简化：如模型未按指引使用 `files`
命令，需从历史 commit 恢复 Shell 变更保护。系统提示词回退到历史版本即可。

## 未决问题

1. edit 是否需要 fuzzy 归一化匹配？v1 仅精确匹配；若真实模型调用中
   `oldText` 与文件内容存在不可见差异（智能引号、行尾空白），可加
   pi-agent 的 `normalizeForFuzzyMatch` 作为 fallback。
2. write 内容需要写入字面 `EOF` 行时的处理：tree-sitter 与 bash 一样按首个
   匹配行终止，此时只能换标记或拆分写入；是否需要支持模型自选标记（如
   `<<'MARK' ... MARK`）？
3. 是否需要 `files` 子命令的 usage 帮助输出（`files help` / 空参数）？

## 决策记录

**状态**：PROPOSED

**日期**：2026-08-03

**决策**：待项目 owner 评审。

**2026-08-04 评审补充（模块组织）**：grammar/facts 与 handler 同放
`handlers/files.py` 单模块，不新增 `_capability/files/` 能力域——`files`
是 Runtime-owned 命令而非 Repertoire 驱动的能力域，facts 无跨层共享，
与 `cd.py`/`export.py`/`tools.py` 的目录语义一致。

## 参考

- `docs/rfcs/approved/RFC-0002-workspace-capability-view.md`
- `docs/rfcs/approved/RFC-0003-tool-capability-commands.md`
- `docs/rfcs/approved/RFC-0008-shell-ast-pluggable-policy-and-guided-exploration.md`
- `/Users/huangzhenghao/Code/Agents/pi/packages/coding-agent/src/core/tools/write.ts`
- `/Users/huangzhenghao/Code/Agents/pi/packages/coding-agent/src/core/tools/edit.ts`
- `/Users/huangzhenghao/Code/Agents/pi/packages/coding-agent/src/core/tools/edit-diff.ts`
- `src/cli_agent/runtime/_capability/tools/grammar.py`
- `src/cli_agent/runtime/_capability/view.py`
