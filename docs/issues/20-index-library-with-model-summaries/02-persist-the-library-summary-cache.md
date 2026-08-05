# feat(library): persist the summary cache

**状态：** resolved

## 背景

Library 摘要是外部模型调用的结果。如果每次 Runtime open 都重新生成，未变化
文件和多个 Workspace 中的相同内容会重复产生延迟和调用成本。RFC-0011 选择在
`~/.config/cli-agent/state.sqlite3` 中缓存成功摘要，同时把该文件定义为
cli-agent 的应用状态数据库，而不是绑定 Library 的专用数据库，以便后续通过
独立表扩展 Session History 等能力。

缓存身份已经收敛为 fingerprint。模型、adapter 和 prompt 版本不参与缓存键；
未来若摘要契约必须整体失效，应通过显式数据库 migration 清除这类派生记录。

## 影响

完成后，文件和目录摘要可以跨 Runtime、跨 Workspace 复用。SQLite 只持久化成功
结果，不保存 Library 原文、parser 输出、凭证、失败状态或 pending job。数据库
生命周期和 migration 也会形成可扩展的应用状态边界，而不会预建 Session History
schema。

## 变更

- 建立私有应用状态数据库 adapter，默认打开
  `~/.config/cli-agent/state.sqlite3`，并允许测试注入隔离路径。
- 首次创建应用目录和数据库时分别使用 `0700` 与 `0600`；不静默修改已有目录
  权限。
- 使用 `PRAGMA user_version` 管理显式 migration，并创建：

  ```sql
  CREATE TABLE library_summary_cache (
      fingerprint TEXT PRIMARY KEY,
      subject_kind TEXT NOT NULL
          CHECK (subject_kind IN ('file', 'directory')),
      summary TEXT NOT NULL,
      created_at TEXT NOT NULL,
      last_used_at TEXT NOT NULL
  );
  ```

- 将 `subject_kind` 作为 inspection/diagnostic 元数据；缓存命中只查询已经包含
  `file`/`directory` domain separator 的 fingerprint。
- 提供最小 cache API：批量读取已发现 fingerprint、成功结果 upsert、命中时更新
  `last_used_at`；不暴露通用 SQL 或 Library source 查询接口。
- 使用短事务和有界 `busy_timeout`，模型调用与文件解析不得位于事务内；首期保留
  SQLite 默认 rollback journal，不启用 WAL。
- 并发进程写入相同 fingerprint 时以主键和 upsert 收敛，允许提交前发生偶发重复
  模型调用。
- 增加 migration、权限、cache hit/miss、跨 Workspace 复用、并发 upsert 和“不
  保存敏感原文”的测试。

## 验收标准

- [ ] 相同 fingerprint 只保留一条成功摘要，并可在新 Runtime 中命中。
- [ ] 模型名称、provider adapter 和 prompt 不构成 cache API 或 schema 的一部分。
- [ ] SQLite 不保存正文、parser 输出、API key、pending job 或失败结果。
- [ ] 数据库 migration 可重复执行，事务不包围模型或文件解析工作。
- [ ] 测试不读取或修改用户真实的 `~/.config/cli-agent/state.sqlite3`。

