# feat(runtime): parse files command grammar from shell AST

**状态：** resolved

## 背景

RFC-0009 引入 `files write` / `files edit` 作为文件修改的首选通道。与 `tools`
一样，`files` 是 Runtime-owned custom command，其语法解析必须基于 Shell AST
的结构化模式匹配（`command.command_head` + `match root.argv, root.redirects`），
不能用正则重新处理 `raw_command`，也不允许非法形态落 Shell fallback。

当前 `tools/grammar.py` 已经确立了该模式（`ShellWord.value`、`HereDocRedirect`
等），`files` 需要一套同样精确的语法契约：path 静态可求值、heredoc 标记固定、
operator 精确为 `<<`。

## 影响

完成后，`files write` 与 `files edit` 的合法形态与非合法形态有唯一、可测试的
定义：合法调用得到结构化的 path/content（或 JSON payload）事实，非法调用得到
统一的 usage 错误；pipeline、`&&`、多重重定向、prefix assignment、动态路径等
一律不匹配且不落 Shell。后续 write/edit handler 可以直接消费解析结果。

## 变更

- 新增 `files` 命令的语法解析（纯函数，与 `tools/grammar.py` 同构），返回
  结构化 facts 或 usage 错误。facts 与解析函数与后续 handler 同放
  `handlers/files.py` 单模块（命令家族一个文件，与 `cd.py`/`export.py`/
  `tools.py` 一致）；**不新增 `_capability/files/` 能力域**——`files` 没有
  catalog/discovery/worker 等域内共享，`_capability/` 下的包都是
  Repertoire 驱动的能力域，`files` 只是 Runtime-owned 命令，不属此语义。
  - `command.command_head != "files"` 时返回 `None`（非本命令）；
  - root 必须是无 prefix assignments 的 `SimpleCommand`；
  - `argv` 恰为 `(ShellWord(value=...), ShellWord(value=path))` 两元组，
    path 的 `value` 必须静态可求值（含 `$VAR` 等动态词时拒绝）；
  - `redirects` 恰为单个 `HereDocRedirect`，`operator == "<<"`，delimiter 的
    `value` 分别为 `EOF`（write）与 `EDI`（edit），`<<-`、`<<<` 一律拒绝；
  - write 的 content 为 `heredoc.body.text` 原文（含终止符前换行，不展开）；
  - edit 的 payload 为 UTF-8 JSON 文档，`json.loads` 解析，`edits` 必须是非空
    数组且每项含非空 `oldText` 与 `newText`；支持单行引号形式
    `files edit <path> '<json>'` 作为便捷写法。
- 定义统一 usage 错误文案，覆盖：缺子命令、未知子命令、缺 heredoc、终止符
  不匹配、动态路径、非法 JSON、空 edits、空 oldText。
- 单元测试：上述每个合法/非法分支，含 `'my file.py'` 带空格路径、`<<EOF` /
  `<<'EOF'` / `<<"EOF"` 三种 delimiter 写法、内容含独立行 `EOF` 的终止行为、
  空内容、多重重定向与 pipeline 不匹配。

## 验收标准

- [ ] 解析完全基于 AST 模式匹配，无正则处理 `raw_command`。
- [ ] `files write <path> <<'EOF' ... EOF` 与 `files edit <path> <<'EDI' {...} EDI`
      返回结构化 facts。
- [ ] 所有非法形态返回 usage 错误（含具体原因），不落 Shell fallback。
- [ ] 与 `tools/grammar.py` 同构的模块级纯函数，位于 `handlers/files.py`，
      不新增 `_capability/files/` 能力域；无新增依赖。
