# refactor(library): migrate Library to Workspace Filesystem

**状态：** resolved

## 背景

Library Catalog 在 Runtime open、普通模型请求前 reconcile 和后台摘要完成时持续
调用 Host `iterdir/stat/read_bytes`，并直接原子写 `index.md`。它是 Runtime open
后仍长期访问 Capability View 的最大消费者，也是 Backend Workspace close 顺序的
关键依赖。

参考：[RFC-0012](../../rfcs/approved/RFC-0012-backend-workspace-and-capability-view-decoupling.md)。

## 影响

完成后，Library source discovery、fingerprint、失效检测、摘要读取和 index
projection 全部位于 Backend Workspace 命名空间。SQLite summary cache 仍属于
Host application state，且不保存 Library 原文或 Backend transport 数据。

## 变更

- 让 Library parser 接受 Backend Filesystem 读取的 bytes 与 logical filename，
  不接收 Host Path。
- 将目录扫描、mtime/size snapshot、source read、dirty path reconcile 与 index
  write 迁移到异步 Workspace Filesystem/Bound View。
- Library entry path 统一为 managed relative path；File mutation callback 只传
  logical path。
- 保持 fingerprint、SQLite cache key、pending/stale/ready/failed/unsupported、
  自底向上目录摘要和非阻塞串行 worker 行为。
- 确保 Library worker close 后不再调用 Backend Filesystem，为 Runtime close 顺序
  提供可验证 barrier。
- 扩展端到端测试，覆盖无 Host mirror、外部 Backend mutation、Runtime Files
  mutation、重启缓存和 close cancellation。

## 验收标准

- [ ] Library Catalog/worker 不直接读取或写入 Host Workspace Path。
- [ ] 既有 summary cache 与状态机语义无回归。
- [ ] Backend mutation 能在下一次普通模型请求前被 reconcile。
- [ ] worker close 后不会访问已关闭 Backend Workspace。

