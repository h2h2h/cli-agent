# feat(library): generate directory summaries bottom up

**状态：** resolved

## 背景

文件摘要只能解释单个文件。上一级 `index.md` 还需要说明一个子目录整体包含什么、
何时应当进入查阅。RFC-0011 决定目录摘要也由模型生成，但不读取整个后代正文；
它只消费排序后的直接子项名称、类型和摘要。

目录摘要引入明确依赖：子文件与子目录先进入终态，父目录才能确定输入并生成当前
摘要。该依赖需要自底向上调度，避免在每个子项完成时反复生成尚不稳定的父摘要。

## 影响

完成后，每个目录会获得语义导航描述，父索引可以在不读取后代正文的情况下概括
子目录。文件或下级目录变化会沿祖先链使目录摘要失效并最终收敛，但所有模型调用
仍在同一个非阻塞串行 worker 中执行。

## 变更

- 当一个目录的所有直接子项达到终态后，构造排序输入：
  `child_name`、`child_kind`、`child_summary`；没有可用摘要的 `failed` 或
  `unsupported` 子项使用 Runtime 固定 `unavailable` 文本。
- 计算目录 fingerprint：

  ```text
  hash(
      "directory",
      ordered(child_name, child_kind, child_summary),
  )
  ```

- 先按 fingerprint 查询 SQLite；cache miss 才把目录任务加入现有串行 worker。
- 目录 prompt 只接收直接子项事实，要求生成约 200 tokens 的目录描述；与文件摘要
  一样，不设置输入/输出 budget 或额外结果检查。
- 以目录深度从下到上推进任务：文件摘要和最深目录先完成，父目录只在直接子项
  输入稳定后生成；根 Library 目录也遵循同一规则。
- 目录摘要成功后缓存 `subject_kind=directory`，刷新自身 frontmatter、父目录条目
  以及必要的祖先索引。
- 子项集合、名称、类型或摘要变化时，使当前目录与祖先目录进入 `pending` 或
  `stale`；模型或 prompt 变化不主动失效已有目录缓存。
- 接受一次叶子变化可能触发多个祖先模型调用，不增加并行调度、持久 job queue 或
  lease 机制。
- 增加多层目录、排序稳定性、cache hit、失败子项、祖先级联、stale-to-ready 和
  单 worker 调用顺序测试。

## 验收标准

- [ ] 目录模型输入只包含排序后的直接子项名称、类型和摘要。
- [ ] 父目录不会在直接子项输入尚未进入终态时生成摘要。
- [ ] 多级目录严格自底向上收敛，且每个稳定 fingerprint 最多保存一条缓存。
- [ ] 文件变化会使必要的祖先目录摘要失效，不会读取或拼接后代正文。
- [ ] 目录摘要生成不阻塞 Runtime open 或普通 Agent 对话。

