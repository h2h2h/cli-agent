# feat(library): reconcile source changes

**状态：** resolved

## 背景

Library source 既可能由 Runtime 的 `files write`/`files edit` 修改，也可能在
cli-agent 运行期间被外部编辑器直接修改。只在 Runtime open 扫描会让索引长期展示
过期摘要；只依赖文件命令 hook 又无法覆盖外部修改。RFC-0011 选择内部 `dirty`
失效事实与正常模型请求前 reconcile 相结合，不引入 watcher 或新命令。

`dirty` 与公开状态不是同一概念：它只表示路径必须重新检查。fingerprint 计算后，
没有旧摘要的条目公开为 `pending`，当前 Runtime 持有旧摘要的条目公开为 `stale`。

## 影响

完成后，Runtime 控制的 Library 写入会被精确失效；外部增加、编辑或删除在下一个
普通 Model Request 前可见。索引会先明确展示 `pending`/`stale`，后台 worker 再
生成文件及祖先目录摘要。正常对话不会等待摘要完成。

## 变更

- 为 `_LibraryCatalog` 增加内存 source snapshot 和 `dirty_paths`：snapshot 至少
  保存 path、`mtime_ns`、size 与已知 fingerprint。
- 让 `files write` 和 `files edit` 在成功完成原子修改后通知 Catalog 精确目标路径；
  失败操作不得触发 dirty，也不改变现有 Files command 语法或输出。
- 在每次普通 Agent Model Request 前执行异步 reconcile hook：
  - 对 dirty 路径强制重新检查；
  - 对其他路径比较成员关系、`mtime_ns` 与 size；
  - 新增、删除或元数据变化时重新读取并计算 fingerprint；
  - 内部 Library 摘要请求不得递归触发该 hook。
- 对齐状态转换：
  - 新内容没有旧摘要时为 `pending`；
  - 当前 Runtime 持有旧摘要时为 `stale` 并继续展示显式过期摘要；
  - cache hit 可直接恢复 `ready`；
  - 删除条目立即从 Catalog 与索引移除。
- 文件变化时失效所在目录和全部祖先目录；先刷新索引状态，再排队文件和自底向上
  目录摘要，不延迟当前普通模型请求。
- Runtime open 始终执行完整 source 扫描和内容 hash；活动期快速比较接受无法发现
  同时保持 size 与 `mtime_ns` 的刻意外部修改，不增加文件 watcher。
- 覆盖 Workspace override、whiteout、外部新增/编辑/删除、Runtime Files 写入、
  dirty-to-pending、dirty-to-stale、祖先失效和模型请求时序测试。

## 验收标准

- [ ] 成功 `files write`/`files edit` 修改 Library 后精确路径进入内部 dirty 集合。
- [ ] `dirty` 不出现在 `LibraryEntry.status` 或任何 `index.md` 中。
- [ ] 外部普通编辑最迟在下一个普通 Model Request 前反映到索引状态。
- [ ] reconcile 不等待模型摘要，也不会被内部摘要 request 递归触发。
- [ ] 首期不增加 watcher、轮询后台任务或任何 Library 命令。

