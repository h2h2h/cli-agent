# feat(library): define catalog facts and text parsers

**状态：** resolved

## 背景

[RFC-0011](../../rfcs/proposed/RFC-0011-non-blocking-model-generated-library-indexes.md)
要求 Runtime 从有效 Capability View 发现 Library，而不是从 SQLite 或已有
`index.md` 恢复成员关系。当前 `.workspace/library` 已可见，但 Runtime 没有
Library Catalog、条目状态、文件 parser 或内容 fingerprint，后续缓存、索引和
后台摘要都缺少统一的事实边界。

首期只支持 UTF-8 编码的 `.md` 和 `.txt`。PDF、PPT 等格式暂不解析，但文件读取
不应直接耦合到 Catalog 或摘要 worker，以便后续通过新的 parser 实现扩展格式。

## 影响

完成后，Runtime 将拥有引用稳定的 Library Catalog，可以确定性描述有效
Library 中的文件和目录、来源层、shadow 关系、fingerprint 与公开状态。支持的
文本文件通过统一 parser 产生完整摘要输入；不支持或无法解析的文件只影响自身。
该 issue 不调用模型、不创建摘要缓存，也不生成新的 Agent 命令。

## 变更

- 在 `_capability/library` 职责边界中定义不可变 `LibraryEntry` facts，至少包含：
  - 逻辑路径、`file`/`directory` 类型；
  - `repertoire`/`workspace` provenance 与 shadow 事实；
  - fingerprint、`ready`/`pending`/`stale`/`failed`/`unsupported` 状态；
  - 可选摘要和有界错误原因。
- 定义 `LibraryFileParser` 协议，提供 `supports(path)` 与异步 `parse(path)`；首期
  registry 只注册一个支持 UTF-8 `.md`、`.txt` 的 text parser。
- text parser 返回完整规范化文本，不截断、不分块、不执行摘要，也不设置输入
  budget；无效 UTF-8 和读取错误形成条目级失败事实。
- 从 `_CapabilityView` 递归发现有效 `.workspace/library`：
  - 排除 Runtime 生成的 `index.md`；
  - 保留 lower/upper 合并、whiteout、copy-up 和 provenance 的现有语义；
  - 不对 Repertoire 中的 `library/memory` 添加特殊校验或 diagnostic；
  - `resources` 与 `memory` 仅作为普通目录参与扫描。
- 定义稳定 fingerprint helper：
  - 文件为 `hash("file", source_bytes_digest)`，不包含文件名、路径、模型、
    prompt 或 provenance；
  - 目录 fingerprint 接口接收排序后的直接子项名称、类型和摘要，具体调度在
    issue 05 接入。
- 将可变 `_LibraryCatalog` 引用加入 `_RuntimeResources`，保持 Catalog 自身为私有
  Runtime-owned 资源；首期 reconcile 只建立事实，不启动模型任务。
- 增加 Catalog、parser、Capability View merge、`index.md` 排除和 fingerprint
  单元测试。

## 验收标准

- [ ] `.md`、`.txt` 文件能产生完整 UTF-8 parser 输出和内容 fingerprint。
- [ ] 相同文件内容在不同名称、provenance 或 Workspace 下具有相同 fingerprint。
- [ ] 不支持扩展名、无效 UTF-8 和读取失败只改变对应条目的状态。
- [ ] 生成的 `index.md` 不会被重新发现为 Library source。
- [ ] Repertoire 与 Workspace 的合并事实复用 `_CapabilityView`，不增加目录来源限制。

