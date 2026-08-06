# refactor(backend): route Files and cwd through Workspace Filesystem

**状态：** resolved

## 背景

`_FileHandler` 当前用 Host `Path`、`mkdir`、`read_bytes`、`mkstemp` 与
`os.replace` 执行 write/edit；`cd` 也用 Host `Path.exists/is_dir` 验证目标。
即使 Shell 已进入 Backend，这两条路径仍会读写 Host 命名空间。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，Files 与 Shell 操作同一个 Workspace Filesystem，`cd` 只修改 Kernel cwd
但通过 Backend 验证目录。File grammar、精确 edit、BOM/CRLF 与输出语义保持由
Runtime Handler 定义，不会退化为 Shell 命令。

## 变更

- 完成 Local Workspace Filesystem 的 stat/list/read/write/edit/remove 与统一错误
  facts；Local write/edit 继续提供同目录原子替换和 mode 保留。
- 新增通用 `_FilesystemExecution`，只在 `run()` 中调用异步 filesystem operation，
  并保留 cancel-before-run 零副作用。
- `_FileHandler` 只解析 command facts、创建 filesystem request 与格式化结果；删除
  Host `Path` I/O、临时文件和具体 Capability View 依赖。
- `cd` 通过 filesystem stat 判断存在性和 directory kind，成功后再调用 Kernel
  `set_cwd`；Backend path 由 Backend 解释。
- 将 Library dirty notification 改为 logical Backend path callback，暂不要求
  FileHandler 知道 `_LibraryCatalog` concrete type。
- 保持 Files serial barrier、Custom route、错误文案与 model-visible Snapshot。

## 验收标准

- [ ] Files 与 `cd` Handler 不通过 Host `Path` 查询 live Workspace。
- [ ] write/edit 的原子性、mode、BOM、CRLF 与精确匹配行为无回归。
- [ ] managed capability path 写入不穿透 lower。
- [ ] `cd` 和 Files 看到 Shell 创建的目录与文件。

