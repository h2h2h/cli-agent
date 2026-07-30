# docs(environment): fix the `.workspace/env` format / 固定 `.workspace/env` 格式

**Status / 状态：** pass

## English

### Background

The accepted milestone contract makes `.workspace/env` the user-maintained,
Workspace-level name-to-value source, but deliberately does not infer an
on-disk encoding. The format becomes persistent user data and must be fixed
before a loader or compatibility behavior is implemented.

### Options

| Format | Parsing | Editing | Multiline/empty | Atomic update | Main cost |
|---|---|---|---|---|---|
| dotenv file | `python-dotenv` | Familiar | Quoted values and explicit empty values | Whole mapping | Requires one parser dependency |
| One file per variable | Direct filesystem mapping | Simple per value | Native | Per variable | More files and strict directory validation |
| JSON/TOML mapping | Standard structured parser | Good for small maps | Escaped | Whole mapping | Whole-file conflicts and typed-value pitfalls |

### Decision

Use one regular dotenv file parsed by `python-dotenv`:

```text
.workspace/
└── env
```

```dotenv
API_BASE_URL=https://example.test/api
GITHUB_TOKEN="replace me"
REPORT_FORMAT=json
```

- Runtime open creates an empty `.workspace/env` regular file when absent.
- Symbolic links, directories, sockets, devices, and other non-regular objects
  at that path are rejected.
- The file is strict UTF-8 and uses `python-dotenv` syntax. Blank lines,
  comments, quoted values, multiline quoted values, and the optional `export`
  prefix are supported.
- Every variable must use `KEY=VALUE`; a bare `KEY` is rejected. `KEY=`
  represents an empty string.
- Variable names come from the dotenv parser rather than a Runtime-owned regex.
  Names and values may not contain NUL.
- Duplicate keys follow dotenv's ordered mapping behavior: the last assignment
  wins. Names remain case-sensitive.
- Interpolation is disabled. `${NAME}` remains literal Workspace configuration
  rather than consulting earlier entries or the Host environment.
- Any syntax or decoding error fails Runtime open before the mapping is
  published. Diagnostics identify the line and path, not its contents.
- Runtime readers open one coherent file snapshot. Writers should create a
  complete temporary dotenv file beside it and atomically replace
  `.workspace/env`.

There is no format autodetection, old-directory migration, Secret encryption,
Host grant, or Runtime mutation of this file.

## 中文

### 背景

已接受的 milestone 契约将 `.workspace/env` 定义为用户维护的 Workspace 级
name-to-value 来源，但没有擅自推断磁盘编码。该格式会成为持久用户数据，因此
必须先固定契约，再实现 loader 或兼容行为。

### 方案

| 格式 | 解析 | 编辑 | 多行/空值 | 原子更新 | 主要成本 |
|---|---|---|---|---|---|
| dotenv 单文件 | `python-dotenv` | 熟悉 | quoted value 与显式空值 | 整体 mapping | 增加一个 parser 依赖 |
| 每变量一文件 | 直接文件系统 mapping | 单值简单 | 原生 | 单变量 | 文件较多且需严格校验目录 |
| JSON/TOML mapping | 标准结构化 parser | 小 mapping 友好 | 需要转义 | 整体 mapping | 整文件冲突和类型陷阱 |

### 决策

使用由 `python-dotenv` 解析的单个普通 dotenv 文件：

```text
.workspace/
└── env
```

```dotenv
API_BASE_URL=https://example.test/api
GITHUB_TOKEN="replace me"
REPORT_FORMAT=json
```

- Runtime open 在缺失时创建空的 `.workspace/env` 普通文件。
- 该路径若为符号链接、目录、socket、device 或其他非普通对象，则拒绝 open。
- 文件使用 strict UTF-8 和 `python-dotenv` 语法，支持空行、注释、quoted
  value、多行 quoted value 与可选 `export` 前缀。
- 每个变量必须使用 `KEY=VALUE`；裸 `KEY` 非法，`KEY=` 表示空字符串。
- 变量名由 dotenv parser 决定，不再维护 Runtime 自有正则。名称和值均不得
  包含 NUL。
- 重复 key 遵循 dotenv 的有序 mapping 行为：最后一次赋值获胜。名称区分
  大小写。
- 禁用 interpolation；`${NAME}` 保留为 literal Workspace 配置，不读取前序
  entry 或 Host environment。
- 任一语法或解码错误都在 mapping 发布前令 Runtime open 失败。诊断只标识
  行号和路径，不包含内容。
- Runtime reader 读取一个一致文件快照。writer 应在同目录写出完整临时 dotenv
  文件，再原子替换 `.workspace/env`。

不定义格式自动探测、旧目录迁移、Secret 加密、Host grant 或 Runtime 回写。
