# feat(runtime): parse shell commands into immutable AST

**状态：** resolved

## 背景

commit `4d0216b` 已在
`a2ea25d18ea342b3e2890c9dbcdaa13a96ce965b` 基础上完成 Shell AST parser：组合操作符、
重定向、subshell 和 command substitution 由同一个语法边界表达，Policy、Router、
Scheduler 和 Capability View 不再需要各自重建 Shell 结构。

RFC-0008 保留这一实现方向。本 issue 只记录已经完成并通过评审的 parser 能力；新版
RFC 新增的“parse failure 必须在 Router 和 Policy 之前停止”属于执行主链路边界，放入
issue 02 实施。

## 影响

完成后，每个 `exec` 请求可以得到统一、不可变、带来源位置的 `ShellParseResult`。
下游能够消费同一份 AST，不再维护第二套 Shell 结构扫描逻辑。parser 对 malformed
command 返回稳定的 `root=None`，具体 ToolResult 与短路行为由调用方负责。

## 变更

- 使用 `tree-sitter-bash` 实现 `parse_shell_ast`。
- 用不可变节点表达 simple command、pipeline、`&&`、`||`、`;`、重定向、后台执行、
  subshell 和 command substitution，并保留 token 与 source span。
- 对 tokenization failure 和 tree-sitter error node 返回稳定的 `root=None`。
- 将合法但 Runtime 未进一步解释的语法保留为 `UnsupportedCommand` 等成功解析结果，
  允许进入 Shell fallback。
- 删除旧 `parse_shell_command` 及重复的字符串扫描，不保留兼容层。
- 增加 parser 节点、来源位置、派生语法事实和 malformed input 单元测试。

## 验收标准

- [x] 支持的 Shell 结构均由 AST 表达，并保留稳定的来源位置。
- [x] `ShellParseResult` 与 AST 节点为不可变数据。
- [x] malformed input 返回稳定的 `root=None`，不抛出 parser exception。
- [x] 合法但未解释的语法由 `UnsupportedCommand` 表达。
- [x] 旧 `parse_shell_command` 已删除，下游统一接收 `ShellParseResult`。
- [x] parser 单元测试覆盖节点、来源位置、派生事实和 malformed input。
