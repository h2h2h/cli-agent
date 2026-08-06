# refactor(capability): migrate Tool and Skill Catalogs to Bound View

**状态：** resolved

## 背景

Tool 与 Skill Catalog 当前直接遍历 `_CapabilityView.root` 下的 Host 目录，使用
`Path.read_text()` 解析 source，并把 Host Path 保存进 entry。Remote Backend 无法
提供这些 Path，Tool request 也不能把它们序列化为 Remote worker 可用路径。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，Tool/Skill discovery、validation、provenance 与 index projection 只依赖
Bound Capability View。Catalog entry 保存 managed relative path 或 Backend logical
path，Runtime-local list/inspect 和 system message snapshot 不需要访问 live Host
文件系统。

## 变更

- 将 Tool、Skill parser 的文件输入收敛为 bytes/text 加 logical filename，不要求
  parser 自行打开 Host Path。
- 让 Tool/Skill Catalog reconcile 使用异步 Bound View list/read/inspect 和
  Workspace Filesystem projection write。
- 将 `ToolEntry.path`、Skill source path 与 companion documentation path 迁移为
  logical/relative path facts。
- 保持 Python AST validation、frontmatter validation、parallel-safe、provenance、
  shadow 与 generated `index.md` 内容不变。
- `tools list/inspect` 和 system message 继续读取 immutable Runtime-open Catalog
  snapshot；本 issue 不增加动态 refresh。
- 增加无 Host mirror 的 fake Bound View 测试和静态 Host Path 回归。

## 验收标准

- [ ] Tool/Skill Catalog 不遍历或读取 live Host Path。
- [ ] Catalog entries 不保存只对 LocalBackend 有效的路径。
- [ ] 现有 validation、index、system message 与 scheduling facts 无回归。
- [ ] Remote-style in-memory Bound View 能完成完整 reconcile。
