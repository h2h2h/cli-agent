# feat(environment): load the Workspace environment once / 一次加载 Workspace 环境

**Status / 状态：** pass

## English

### Background

The Runtime needs one complete Workspace custom-environment mapping before any
Session exists. Workspace file edits must not partially mutate an active
Runtime.

### Changes

- Load `.workspace/env` during Runtime open using only the convention accepted
  in issue 00.
- Validate the complete input before publishing an immutable Runtime-owned base
  mapping.
- Fail Runtime open on malformed input with a diagnostic that identifies the
  dotenv line and path but does not dump values.
- Treat a valid empty configuration as an empty mapping.
- Load once per Runtime; later file edits affect only a later Runtime open.
- Keep the mapping private and avoid adding a Host Environment Grant parameter
  or a public mutable configuration object.
- Add focused tests for valid, empty, malformed, conflicting, and open-cleanup
  behavior.

## 中文

### 背景

Runtime 必须在任何 Session 存在前获得完整的 Workspace 自定义环境 mapping。
Workspace 文件修改不得局部改变活动 Runtime。

### 变更

- 在 Runtime open 时只按 issue 00 接受的约定加载 `.workspace/env`。
- 完整校验输入后再发布不可变的 Runtime-owned base mapping。
- 非法输入令 Runtime open 失败；诊断标识 dotenv 行号和路径，但不转储值。
- 合法空配置视为空 mapping。
- 每个 Runtime 只加载一次；后续文件修改只影响后续 Runtime open。
- mapping 保持私有，不添加 Host Environment Grant 参数或公共可变配置对象。
- 添加 valid dotenv、empty、malformed、conflict 和 open-cleanup 聚焦测试。
