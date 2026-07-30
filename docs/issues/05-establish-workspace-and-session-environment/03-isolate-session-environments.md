# feat(environment): isolate custom environments per Session / 隔离每个 Session 的自定义环境

**Status / 状态：** pass

## English

### Background

All Sessions in one Runtime share the Workspace environment source, but their
later exports must not cross Session boundaries or survive Session recreation.

### Changes

- Give each new Environment Session a mutable copy of the Runtime-open
  Workspace environment.
- Keep the mapping owned by the Environment Session rather than AgentLoop,
  Scheduler, Driver, or public Environment Binding.
- Ensure two Sessions can override the same name independently.
- Preserve one Session's mapping across its turns.
- Discard the mapping on Session close; reusing the Host-visible Session ID
  starts from a fresh copy of the Runtime-open base.
- Runtime close releases all Session mappings and never writes them back to
  `.workspace/env`.
- Add deterministic tests for cross-Session isolation, multi-turn retention,
  close/recreate freshness, and Runtime-open snapshot reuse.

## 中文

### 背景

同一 Runtime 中的所有 Session 共享 Workspace 环境来源，但之后的 export
不得跨 Session，也不得在 Session 重建后保留。

### 变更

- 每个新 Environment Session 获得 Runtime-open Workspace 环境的可变副本。
- mapping 由 Environment Session 持有，而不是 AgentLoop、Scheduler、Driver
  或公共 Environment Binding。
- 两个 Session 可以独立覆盖同名变量。
- 同一 Session 跨 turn 保留 mapping。
- Session close 丢弃 mapping；复用 Host-visible Session ID 时从 Runtime-open
  base 的全新副本开始。
- Runtime close 释放全部 Session mapping，绝不写回 `.workspace/env`。
- 添加跨 Session 隔离、多 turn 保留、close/recreate freshness 和 Runtime-open
  snapshot 复用的确定性测试。
