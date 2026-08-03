# feat(prompt): guide shell exploration and file reads

**状态：** resolved

## 背景

模型当前知道可以执行 Shell command，但 system message 没有明确说明如何在大型工作区
中逐步定位文件、收窄读取输出、区分读写边界，以及何时并行独立的只读探索。缺少这些
指引时，模型容易一次读取过多内容，或为普通文件读取编写不必要的临时脚本。

RFC-0008 参考 `docs/references/Codex_Read_Tool_Design_Research.pdf` 的第 6、7 节与
附录 B，只引入当前项目能力能够兑现的静态工作流。

## 影响

完成后，模型会更倾向先搜索再局部读取，主动限制输出，并将独立的只读探索放入同一
批次。指引不依赖 Shell Catalog，也不会把命令推荐描述成安全保证。

## 变更

- 在 system message 中定义 `search -> targeted read -> wider read only when needed`
  的递进探索顺序。
- 给出当前环境可直接执行的 Shell read primitives：`rg --files`、`rg -n`、`cat`、
  `sed -n`、`head`、`tail`、`nl -ba`、`wc -l`、`stat` 和只读 Git 查询。
- 要求输出截断时收窄搜索或改读更小范围，不能重复请求相同的大输出。
- 建议普通文件读取优先使用 Shell read，避免为了打印文件编写 Python 脚本。
- 明确独立只读观察应作为同一 model batch 中分离的 `exec` 调用；有数据依赖时保持
  顺序，不能通过拼接 Shell command 模拟并行。
- 将观察与修改分开：修改前读取精确目标与上下文，修改后检查变更区域或 `git diff`，
  再运行聚焦验证。
- 保持文字静态，不从 Catalog、Policy 或运行期探测生成。
- 不描述项目不存在的专用 read、`apply_patch` 或 workdir 能力。

## 验收标准

- [ ] 指引提供从搜索、局部读取到必要时扩大范围的明确决策顺序。
- [ ] 指引给出当前环境可执行的文件发现、文本搜索、范围读取、行号、大小和 Git 历史
      查询形式。
- [ ] 指引说明输出截断后的恢复方式，以及独立观察和依赖观察的不同调度方式。
- [ ] 指引建立修改前后验证闭环，但不声称不存在的 structured edit、workdir、sandbox
      或安全分类能力。
- [ ] contract tests 固化工作流和能力边界，不只检查抽象关键词。
