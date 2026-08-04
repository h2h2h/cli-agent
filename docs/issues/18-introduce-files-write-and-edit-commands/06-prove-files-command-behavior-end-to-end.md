# test(runtime): prove files command behavior end to end

**状态：** resolved

## 背景

`files write` / `files edit` 的语法、handler、Capability View 集成与提示词
分别由 issue 01-05 实现，issue 07 删除 Shell 变更启发式，但单独的单测无法
证明完整 Runtime 路径：`exec` syscall → AST 解析 → Custom-first routing →
Policy（如配置）→ Scheduler → `_FileHandler` → `prepare_path` → 原子写。
需要聚焦回归证明 `files` 与 `cd`、`export`、`tools`、Shell fallback 在同一
Execution lifecycle 下协同工作，并固定 RFC-0009 的结构约束。

## 影响

完成后，RFC-0009 的关键行为由测试与文档共同固定：`files` 非法语法不落
Shell、`files` 不参与并行调度、in-view 写入不穿透 Repertoire、system
message 指引存在且无 Shell 写文件建议；`_DIRECT_MUTATORS` 及 Shell 变更
启发式已由 issue 07 删除，静态回归防止其重新引入。

## 变更

- 扩展 routing/parallelism 测试，验证：
  - `files write`、`files edit` 命中 Custom route，`files` 头匹配不被
    `./files`、`/bin/files` 等形态误命中；
  - malformed `files`、pipeline、多重重定向、动态路径不落 Shell fallback；
  - `files` 命令 `parallel_safe=False`，不进入并行 batch；
  - 保留命令名 `files` 不可重复注册。
- 扩展 Kernel/Execution 集成测试，验证：
  - `exec("files write ...")` 的 Execution snapshot、退出码与输出块
    （`wrote N bytes to <path>`）完整可观察；
  - `files edit` 成功后 `git diff` 可见的变更与替换数输出；
  - write/edit 对 in-view lower link 写入后 Repertoire 未变、view 层为
    实体文件；
  - 取消/失败路径不产生半截文件（原子写）。
- 扩展 system message 断言：指引段包含 `files write` / `files edit` 用法，
  且明确禁止 `tee`、`sed -i`、`cat >`、`echo >` 等 Shell 写文件方式。
- 静态回归：生产源码不再出现 `_DIRECT_MUTATORS`、`_sed_is_in_place`、
  `_operands`、`_delete_paths` 等已删除 symbol。
- 更新 `docs/architecture.md`（Handler 边界）与 RFC-0009 状态（如评审通过
  则标记 APPROVED），记录删除 `_DIRECT_MUTATORS` 的决定。
- 运行完整 pytest、Ruff、mypy，记录结果供同行评审。

## 验收标准

- [ ] `files` 非法语法与非法形态全部不落 Shell。
- [ ] `files` 与既有命令在同一 Execution lifecycle 下协同，无调度回归。
- [ ] in-view 写入不穿透 Repertoire，失败不留半截文件。
- [ ] 完整 pytest、Ruff、mypy 通过；RFC-0009 状态与文档同步。
