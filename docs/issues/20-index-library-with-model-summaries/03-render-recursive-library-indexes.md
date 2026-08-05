# feat(library): render recursive index projections

**状态：** resolved

## 背景

AEP 通过每级目录的 `index.md` 提供低成本导航，但其标题加预览行和固定行数 chunk
无法表达可靠语义。RFC-0011 保留递归文件系统索引，将 `index.md` 定义为 Catalog
的生成投影：它展示当前直接子项、provenance、状态和摘要，但不承担缓存或事实源
职责。

Runtime open 必须在任何模型调用完成前渲染索引，使新文件立即以 `pending` 可见，
缓存命中的条目立即以 `ready` 可见。

## 影响

完成后，用户和 Agent 可以从 `.workspace/library/index.md` 逐级浏览 Library，且能
明确区分当前、待生成、过期、失败和不支持的摘要。索引只写 Workspace upper，
不会修改 Repertoire；删除索引也不会影响 source 或 SQLite 摘要。

## 变更

- 为 `_LibraryCatalog` 增加确定性 renderer，为 Library 根目录及每个可见子目录
  生成 `index.md`。
- 每个索引使用稳定 frontmatter，并分别列出排序后的直接子目录和文件；条目至少
  展示名称、类型、status、description 和相对链接，文件额外展示 provenance 与
  shadow 事实。
- 定义固定的非 ready 文本：
  - `pending` 表示摘要已排队或等待依赖；
  - `stale` 保留显式标记的旧摘要；
  - `failed` 展示有界失败原因或旧摘要；
  - `unsupported` 引导直接读取 source。
- 不生成 chunk、chunk ID 或文件正文预览，不把已有 `index.md` 解析回 Catalog。
- 对摘要、名称和错误文本执行 Markdown 安全转义；该转义只保护投影结构，不作为
  摘要长度、token 数、空输出或段落结构校验。
- 索引通过同目录临时文件和 `os.replace` 写入 Workspace upper；从最深目录向根
  目录刷新，接受多个索引文件之间短暂的最终一致状态。
- 如果 Repertoire 存在同路径 `index.md`，生成的 Workspace upper 文件正常 shadow
  它，但不得写回 lower。
- 在 Runtime open 的本地关键路径中完成 cache lookup 和首次全量渲染，不启动或
  等待摘要模型。
- 增加递归布局、排序、链接、状态文本、转义、lower shadow、原子替换和重复渲染
  测试。

## 验收标准

- [ ] 每个可见 Library 目录都有只列直接子项的 `index.md`。
- [ ] cache hit 在首次索引中为 `ready`，cache miss 在首次索引中为 `pending`。
- [ ] 索引不包含 chunk、正文预览或作为 authority 使用的隐藏元数据。
- [ ] 所有生成文件只写 Workspace upper，Repertoire 内容保持不变。
- [ ] Runtime open 不等待任何模型调用即可完成首次索引投影。

