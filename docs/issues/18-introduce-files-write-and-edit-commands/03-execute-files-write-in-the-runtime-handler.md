# feat(runtime): execute files write in the runtime handler

**状态：** resolved

## 背景

RFC-0009 的 `files write <path> <<'EOF' ... EOF` 需要 Runtime 内实现：解析
（见 issue 01）、路径解析、Capability View 准备（见 issue 02）、原子写与
结构化输出。当前项目没有不经 Shell 的写文件通道，模型只能依赖 `tee`、
`cat >`、`sed -i` 等，这些写法正是 `_DIRECT_MUTATORS` 无法穷举的根源。

## 影响

完成后，模型可用一条命令新建或覆盖任意 workspace 文件：自动创建父目录、
原子写入（失败不留半截文件）、覆盖时保留原文件权限、输出写入字节数。
in-view 路径自动 copy-up（消费 issue 02 的 `prepare_path`）。失败场景
（路径是目录、NUL 字符、权限错误、usage 错误）返回带原因的 failed
execution，均不落 Shell。

## 变更

- 在 `handlers/file.py`（或同职责模块）实现 `_FileHandler`，`prepare` 消费
  issue 01 的解析结果并分发子命令；本 issue 完成 `files write` 分支：
  - 路径解析：`Path(path)`，相对路径以 `context.cwd` 为基准（与 `cd` 的
    `_target_path` 一致），不做 workspace 强制；
  - 写回前调用注入的 `_CapabilityView.prepare_path`（视图尚未注入时按
    no-op 处理，保证阶段可独立验收）；
  - `mkdir(parents=True, exist_ok=True)` 后原子写：同目录 `mkstemp` +
    `os.replace`，覆盖已有文件时保留原 mode（`stat` 后 `os.fchmod`），新建
    默认 0o644；
  - content 按 UTF-8 编码，`heredoc.body.text` 原文（含终止符前换行，与
    bash heredoc 语义一致），不做任何展开或行尾变换；
  - 成功输出 stdout 一行 `wrote <n> bytes to <path>`（n 为字节数）。
- 复用或提升 `_text_execution` 辅助（当前私有于 `handlers/tools.py`）。
- 单元测试：新建、覆盖、父目录自动创建、字节数与输出格式、路径是目录、
  NUL 字节、目标路径不可写、usage 错误、`<<'EOF'` 三种 delimiter 写法。

## 验收标准

- [ ] `files write` 新建/覆盖文件并自动创建父目录，输出字节数正确。
- [ ] 覆盖已有文件保留原权限；新建文件 mode 合理（0o644）。
- [ ] 内容为 heredoc 原文（含尾换行），无展开、无行尾变换。
- [ ] 失败场景返回 failed execution + stderr 原因，不落 Shell。
