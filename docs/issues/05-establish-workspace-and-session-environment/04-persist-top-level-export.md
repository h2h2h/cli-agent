# feat(environment): persist top-level export in Session memory / 在 Session 内存中保留顶层 export

**Status / 状态：** pass

## English

### Background

An ordinary child-shell export cannot mutate its parent Runtime. AEP obtains
persistent Session behavior by recognizing a narrow top-level export grammar
before launching a child.

### Changes

- Fix and parse only direct top-level `export KEY=VALUE ...` as a structured
  operation; reject malformed assignments without partial mutation.
- Route the operation through the existing parse, policy, immutable Decision,
  admission, lane, normalized result, cancellation, and cleanup contract.
- Execute the mutation in the serial Shell lane so a later Shell Execution sees
  an earlier completed export.
- Update only the creating Environment Session's custom mapping.
- Keep nested exports, shell wrappers, pipelines, compound expressions, command
  substitution, and process-local assignments outside the persistent grammar.
- Do not add persistent `cd`, `unset`, shell expansion, a global state barrier,
  or cross-lane ordering in this issue.
- Add tests for FIFO visibility, invalid atomicity, Session isolation, queued
  cancellation, close, and non-covered Shell forms.

## 中文

### 背景

普通 child-shell export 无法修改父 Runtime。AEP 通过在启动 child 前识别窄
顶层 export grammar，获得持久 Session 行为。

### 变更

- 只把直接顶层 `export KEY=VALUE ...` 解析为 structured operation；非法
  assignment 必须在无部分 mutation 的情况下拒绝。
- 通过现有 parse、policy、不可变 Decision、admission、lane、normalized
  result、cancellation 和 cleanup 契约路由。
- 在串行 Shell lane 中执行 mutation，使后续 Shell Execution 能看到先前已完成
  的 export。
- 只更新创建该操作的 Environment Session custom mapping。
- nested export、shell wrapper、pipeline、compound expression、command
  substitution 和 process-local assignment 不属于持久 grammar。
- 本 issue 不添加持久 `cd`、`unset`、shell expansion、全局 state barrier 或
  cross-lane ordering。
- 添加 FIFO 可见性、非法操作原子性、Session 隔离、queued cancellation、
  close 和非覆盖 Shell 形式测试。
