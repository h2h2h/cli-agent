# feat(library): generate file summaries in the background

**状态：** resolved

## 背景

模型摘要能改善 Library 导航，但 provider 延迟、限流、凭证错误和 context overflow
都不能阻塞 Runtime open 或普通 Agent 对话。RFC-0011 选择由 Runtime 持有一个串行
后台 worker：首次索引先展示 `pending`，worker 随后为缓存 miss 生成文件摘要并
刷新缓存和索引。

摘要契约已经收敛为提示词约束，而不是应用侧预算系统。文件 prompt 只包含 parser
返回的完整内容，要求用约 200 tokens 说明文件内容、覆盖范围和查阅时机；实现不
检查输入大小、输出 token/字符数、空输出或段落数量。

## 影响

完成后，受支持文件会在 Runtime 启动后异步从 `pending` 收敛为 `ready`，模型失败
则只把对应条目标记为 `failed`。成功摘要写入 SQLite，并原子刷新相关索引。普通
Agent Session history 不会包含内部摘要请求，也不会新增任何 Agent 可见命令。

## 变更

- 实现由 `_LibraryCatalog` 拥有的串行摘要队列和 worker task；只为受支持、cache
  miss 且未排队的文件提交任务。
- 使用 Runtime default `ModelProvider` 构造内部 `ModelRequest`：
  - `tools=()`，不允许摘要调用 Tool；
  - 独立 system instruction 将 Library 内容界定为不可信数据；
  - user 内容只包含完整 parser 输出，不包含文件名、绝对路径或 provenance；
  - 提示模型生成约 200 tokens 的纯文本摘要。
- 接受成功 completion，不执行长度、token、空输出或段落结构校验；写入
  `index.md` 时继续复用 renderer 转义。
- provider 报告 context overflow 或其他异常时，将条目标记为 `failed`，通过现有
  Runtime diagnostic seam 发出不包含正文、凭证或其他敏感信息的有界通知。
- 成功后先短事务 upsert SQLite，再在 Catalog mutation lock 下更新条目并刷新文件
  所在目录及祖先索引。
- Runtime open 在首次渲染后启动 worker，绝不等待队列完成；Runtime close 停止接收
  新任务、取消并等待 worker，再继续普通资源清理。
- 内部摘要请求不写入任何 Agent Session history，不使用每 Session/per-turn provider
  override。
- 增加 pending-to-ready、cache hit 不调用模型、provider failure、context overflow、
  diagnostic 脱敏、close cancellation 和 Runtime-open 非阻塞测试。

## 验收标准

- [ ] 冷缓存文件先以 `pending` 可见，Runtime open 在模型未返回时仍能完成。
- [ ] worker 串行调用模型，内部 request 不带 Tools 且不进入 Session history。
- [ ] 成功 completion 被缓存并使相关索引收敛为 `ready`。
- [ ] context overflow 和 provider 异常只使对应文件 `failed`。
- [ ] Runtime close 不遗留 worker task 或未关闭的 SQLite 资源。
