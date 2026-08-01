# feat(mcp): add the mcp dependency and MCP server config facts / 新增 mcp 依赖与 MCP server 配置事实

**Status / 状态：** pass

## English

### Background

MCP discovery uses the official `mcp` SDK, which the architecture decision 09
already approved as a direct Runtime dependency alongside `httpx`. The
Repertoire gains a `_mcp/` tree that is user-owned and never mounted into the
Capability View, so the model cannot read connection descriptions. The MCP
domain needs a pure-data facts leaf mirroring `tools/facts.py` and
`skills/facts.py`, with config validation that stays structural and fails
without trusting authored metadata.

### Changes

- Add `mcp` to the Runtime dependencies in `pyproject.toml` and refresh
  `uv.lock`.
- In `_prepare_repertoire` (`_capability/view.py`), create the top-level
  `_mcp` directory; keep `_CAPABILITY_DIRECTORIES` at the three visible trees
  so `_mcp` is never mounted or Agent-visible.
- Create `_capability/mcp/__init__.py` and `_capability/mcp/facts.py`.
- Define a frozen `MCPServerConfig` with `name`, `transport`
  (`stdio` | `http`), `command` (stdio only), `url` (http only), `env` (a list
  of env variable NAMES, never literal values), and `headers` (header name →
  env variable name).
- Add a `validate_config` helper using `jsonschema` that returns aggregated
  errors as strings instead of raising; validate `name` (lowercase
  letters/digits/hyphens, bounded length, must equal the directory name),
  transport enum, and per-transport required fields.
- Keep `facts.py` free of `_environment/` imports so the import graph stays
  `_environment -> _capability -> leaf modules`.

## 中文

### 背景

MCP 发现使用官方 `mcp` SDK，架构决策 09 已批准其与 `httpx` 一并作为直接运行时
依赖。Repertoire 新增 `_mcp/` 树，它归用户所有且从不挂载进 Capability View，因此
模型无法读到连接描述。MCP 域需要一个镜像 `tools/facts.py` 与 `skills/facts.py`
的纯数据叶子模块，配置校验保持结构性，且失败时不信任作者编写的元数据。

### 变更

- 在 `pyproject.toml` 的 Runtime 依赖中加入 `mcp` 并刷新 `uv.lock`。
- 在 `_prepare_repertoire`（`_capability/view.py`）中创建顶层 `_mcp` 目录；
  保持 `_CAPABILITY_DIRECTORIES` 为三个可见树，使 `_mcp` 永不挂载或对
  Agent 可见。
- 创建 `_capability/mcp/__init__.py` 与 `_capability/mcp/facts.py`。
- 定义冻结的 `MCPServerConfig`，包含 `name`、`transport`
  （`stdio` | `http`）、`command`（仅 stdio）、`url`（仅 http）、`env`
  （env 变量名数组，绝不存字面量值）与 `headers`（header 名 → env 变量名）。
- 新增基于 `jsonschema` 的 `validate_config` 辅助函数，以字符串列表返回聚合
  错误而非抛异常；校验 `name`（小写字母/数字/连字符、长度受限、必须等于目录
  名）、transport 枚举与各 transport 的必填字段。
- 保持 `facts.py` 不 import `_environment/`，使依赖图严格保持
  `_environment -> _capability -> 叶子模块`。
