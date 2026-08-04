# refactor(capability): remove shell mutator heuristics and whiteout reconciliation

**状态：** resolved

## 背景

issue 02 的 `prepare_path` 已让 in-view 与否由目标路径决定，issue 05 的
system message 已指引模型只通过 `files` 命令修改文件。此时 `_CapabilityView`
中基于 `_DIRECT_MUTATORS`（15 个名字）、`_sed_is_in_place` 和
`_operands` 的 Shell 变更启发式失去存在意义：清单无法穷举所有写文件方式，
其 target 推断规则与按路径的 `prepare_path` 语义重复，且 `_delete_paths` /
`_snapshot_deletes` / `_reconcile_deletes` / `_create_whiteout` 只服务于
这些启发式。RFC-0009 决定直接删除，不保留兜底。

## 影响

完成后，`view.py` 不再维护任何"哪些 Shell 命令会改文件"的清单；Shell 命令对
capability 目录的变更仅剩两种 AST 事实处理：输出重定向（`> file`）写前自动
copy-up，其余变更不再有保护。行为变化包括：

- `rm` / `mv` 等 Shell 删除删除的是 view 层 symlink 本身，不穿透 Repertoire，
  但不再创建 whiteout，view 层删除不跨 attach 持久（下次 Runtime open 时
  Repertoire 文件重新出现）。
- `_may_mutate` 简化为仅判断 `contains_output_redirection`，`_write_paths`
  只保留 redirect targets；`_normalize_targets`、`_copy_up`、
  `_reject_symlink_intermediates`、`_remove_whiteout` 与
  `_attach_directory` 的 whiteout 读取保持不变。

## 变更

- 删除 `_DIRECT_MUTATORS`、`_sed_is_in_place`、`_operands`、`_DeleteSnapshot`、
  `_delete_paths`、`_snapshot_deletes`、`_reconcile_deletes`、
  `_create_whiteout`。
- `_may_mutate` 简化为 `command.contains_output_redirection`；
  `_write_paths` 删除 `chmod`/`cp`/`mv`/`dd`/`sed`/`patch` 等 executable
  分支，仅保留 FileRedirect is_output 的 target 收集。
- `prepare_shell` 删除 delete snapshot 与 reconcile 步骤，保留写前 copy-up
  与 mutation lock，docstring 同步更新。
- 清理删除后不再使用的 import（如 `re`）与死代码。
- 测试：
  - 输出重定向到 in-view lower link 仍触发 copy-up（不回归）；
  - Shell `rm`/`mv` 不再产生 whiteout，且删除的 symlink 不穿透 Repertoire
    （固定新行为）；
  - 静态回归：生产代码不再出现上述 symbol；
  - 现有 copy-up、whiteout 移除、`prepare_path` 测试全部保持通过。

## 验收标准

- [ ] `view.py` 及生产代码不存在 `_DIRECT_MUTATORS`、`_sed_is_in_place` 等
      已删除 symbol。
- [ ] `prepare_shell` 只处理 AST 输出重定向的写前 copy-up。
- [ ] Shell 删除行为变化（无 whiteout、不跨 attach 持久）由测试固定。
- [ ] `_copy_up`、`_remove_whiteout`、`_attach_directory` 行为无回归。
