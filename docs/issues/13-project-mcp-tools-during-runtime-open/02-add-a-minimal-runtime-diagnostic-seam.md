# feat(runtime): add a minimal Runtime Diagnostic seam / 新增最小化 Runtime Diagnostic 通道

**Status / 状态：** pass

## English

### Background

The headless Runtime needs a host-directed structured notice channel for
non-blocking reconcile events such as MCP discovery exhaustion. No such channel
exists today: the "diagnostic" prints in the Reference CLI are presentation-only
rendering of model events, not Runtime-to-Host notices. The seam must let the
host decide how to log or present, and must never carry secret values or env
references.

### Changes

- Add a frozen `RuntimeDiagnostic` dataclass with `kind`, `message`, and an
  optional structured `detail`.
- Add `on_diagnostic: Callable[[RuntimeDiagnostic], None] | None` to
  `AgentRuntime.open`, thread it through `_reconcile`, and retain it on the
  opened Runtime so Runtime-open reconcile steps can emit notices.
- Wire the Reference CLI to render received diagnostics to stderr.
- Document that diagnostics carry no env values, credentials, or Secret
  References.
- Keep the change additive: omitting `on_diagnostic` preserves today's silent
  behavior.

## 中文

### 背景

headless Runtime 需要一个面向 host 的结构化通知通道，用于 MCP 发现重试耗尽等
非阻塞的 reconcile 事件。目前不存在这样的通道：Reference CLI 里的
"diagnostic" 打印只是对 model event 的表现层渲染，并非 Runtime → Host 通知。
该通道应让 host 决定如何记录或呈现，且绝不携带 secret 值或 env 引用。

### 变更

- 新增冻结的 `RuntimeDiagnostic` dataclass，包含 `kind`、`message` 与可选的
  结构化 `detail`。
- 为 `AgentRuntime.open` 增加
  `on_diagnostic: Callable[[RuntimeDiagnostic], None] | None`，贯穿
  `_reconcile`，并保留在打开的 Runtime 上，使 open 阶段的 reconcile 步骤可以
  发出通知。
- 将 Reference CLI 接入：收到的诊断渲染到 stderr。
- 文档化：诊断不携带任何 env 值、凭据或 Secret Reference。
- 保持加法式变更：省略 `on_diagnostic` 时维持现有静默行为。
