# Target package structure

Status: approved
Recorded: 2026-07-31

This document is the structural contract for the cli-agent refactor. It fixes
the target package layout, the role of each component, the dependency
invariants that the refactor must establish, and the migration sequence. It
supersedes the structural findings in
[architecture-review-findings](../discussions/architecture-review-findings.md);
the simplification findings there (speculative abstractions, duplicated
semantics, defensive coding) remain valid and are folded into the migration
steps below.

The layout borrows AEP's packaging principle — capability knowledge
(Workspace lifetime, shared across Sessions) is packaged separately from
Session machinery — without adopting AEP's internals. cli-agent's control
plane (Parser → Policy → Approval → Decision → Router → Scheduler → Driver),
its JSON-payload Tool worker, and its bounded output contracts are strictly
stronger than AEP's equivalents and are preserved as-is.

## Naming story

Two domains together form the platform presented to the model: a
directory-based environment through which the Agent uniformly discovers and
invokes capabilities (tools, skills, library).

- **`capability/` is the content of the environment.** The capabilities
  themselves, organized by directory; the Capability View that mounts them;
  and the `.workspace` bootstrap that carries all of it. It answers "what can
  the model discover and invoke". Everything inside is reconciled once at
  Runtime open and shared by all Sessions.
- **`environment/` is the machinery of the environment.** The Session-scoped
  Environment Kernel: the fixed AEP Syscalls (`exec`, `output`, `kill`) and
  the control and execution planes behind them. It answers "how the model
  invokes safely". It is not session management (the Runtime owns the Session
  registry) and not conversation (the loop owns history), which is why the
  name stays `environment` rather than `session`.

The AgentLoop sits outside both: it is the model-side conversation mechanism
and meets the environment-side execution mechanism only through the narrow
`dispatch_batch` seam.

## Target layout

```
src/cli_agent/
├── cli.py  config.py  runner.py  presentation.py     # Reference CLI (Host)
└── runtime/
    ├── __init__.py                # narrowed public surface
    ├── runtime.py                 # AgentRuntime lifecycle + Session registry
    ├── model.py                   # provider-neutral messages/events/Provider
    ├── providers/                 # openai_compatible / scripted
    ├── _loop.py                   # AgentLoop (model-side conversation)
    ├── _system_message.py
    ├── _syscalls.py               # exec/output/kill schemas
    │                              #   (renamed from _builtin_tools.py)
    │
    ├── capability/                # Workspace-lifetime capability domain
    │   ├── workspace.py           # .workspace bootstrap + env loading
    │   │                          #   (from runtime/_workspace.py)
    │   ├── view.py                # Capability View overlay
    │   │                          #   (from runtime/_capability_view.py)
    │   ├── command_parser.py      # command language: CommandParseResult,
    │   │                          #   parser, syntax helpers (from
    │   │                          #   _environment/command_parser.py;
    │   │                          #   see amendment below)
    │   └── tools/
    │       ├── facts.py           # ToolCommand / ToolReference / unified
    │       │                      #   provenance record (pure-data leaf)
    │       ├── catalog.py         # ToolCatalog (from _tool_catalog.py)
    │       ├── grammar.py         # tools command classification
    │       │                      #   (from _tool_commands.py)
    │       ├── environment.py     # Tool venv (from _tool_environment.py)
    │       └── worker.py          # worker script (from _tool_worker.py)
    │       # skills/  library/    # symmetric landing spots for M8 / M9
    │
    └── environment/               # Session-lifetime Environment Kernel domain
        ├── kernel.py              # Session state aggregate (after split)
        ├── protocol.py            # syscall validation/snapshots
        │                          #   (split from kernel, mostly pure)
        ├── supervisor.py          # Driver supervision + approval
        │                          #   coordination (split from kernel)
        ├── policy.py  routing.py  scheduler.py  execution.py
        ├── drivers/               # kept as a subpackage: real polymorphic
        │   │                      #   seam, grows with MCP
        │   ├── base.py  shell.py  tool.py  executions.py
        │   # custom.py deleted (_ResolvedCustomDriver pass-through)
        └── commands.py            # registry + builtins flattened into one
```

## Component roles

### `capability/`

Workspace-scoped capability knowledge. Reconciled once at Runtime open;
Sessions receive references, never copies to mutate. Owns provenance,
validation, whiteouts, generated indexes, and the Tool dependency venv.
`tools/facts.py` is a pure-data leaf module: it imports nothing from
`environment/`, which is what breaks the potential import cycle between
`grammar.py` (consumes `CommandParseResult`) and `parsing.py` (annotates
`CommandParseResult.tool: ToolCommand | None`).

### `environment/`

The Session-scoped Kernel domain. One Kernel per Session, owning cwd, the
Session environment copy, the Scheduler, the Handle namespace, Execution
States, and Driver lifecycle.

### `environment/commands.py` — Runtime-owned command dispatch table

Answers "who defines this command's behavior", *before* routing. A registry
mapping an exact command head to a Runtime-trusted `_CustomCommandSpec`
(`prepare` + scheduling rule), consulted before the Shell fallback.

It exists because some commands cannot be delegated to a child Shell: `cd`
and `export` must mutate the Session's own cwd and environment table, and
child-process state changes cannot propagate back to the parent. Their
`prepare` receives the `_DriverContext` hooks (`set_cwd`, the Session env
mapping) and performs the mutation in-process.

It knows nothing about policy, approval, scheduling, or output bounds. It
grows along the *command vocabulary* dimension: a new Runtime-owned command
(e.g. `unset`) is one new registry entry.

### `environment/drivers/` — execution-plane backend seam

Answers "how an admitted Decision actually runs", *after* routing. The only
place that performs side effects, consuming immutable Execution Decisions
without re-parsing or re-authorizing.

- `base.py`: contracts — `_ExecutionDriver.prepare` (prepare without
  starting), `_DriverExecution.run/cancel` (owns one concrete execution's
  resources), `_DriverContext`, `_ExecutionOutcome`.
- `executions.py`: reusable execution primitives — `_InlineExecution`
  (in-process cooperative handler) and `_ProcessExecution` (child process,
  process group, stream capture, grace-period cancellation).
- `shell.py`: Shell command family; wraps the child Shell in Capability View
  copy-up/whiteout preparation.
- `tool.py`: Tool command family; list/info degrade to inline text
  executions, run assembles the JSON payload and spawns the venv worker.

Custom commands from `commands.py` also produce `_InlineExecution`: both
components feed the same `_DriverExecution` contract, so the supervisor
applies one backend-neutral wait/output/cursor/kill/cleanup discipline with
no driver-type branches in the Kernel.

This seam grows along the *execution backend* dimension: MCP adds
`drivers/mcp.py` without touching `commands.py`.

### `_syscalls.py`

The fixed model-visible syscall schemas (`exec`, `output`, `kill`). Renamed
from `_builtin_tools.py`: "built-in tool" collides with the built-in custom
commands (`cd`, `export`), and CONTEXT.md already names this concept "AEP
Syscall".

## Dependency invariants

The refactor is complete when these hold and are enforced by review:

```
cli layer      → runtime public surface
_loop          → environment + model
environment    → capability          (one-way; capability never imports environment)
providers      → model
capability/tools/facts.py            (imports nothing outside stdlib/typing)
```

The test for a misplaced module: if anything under `capability/` reaches for
a Session concept, it belongs elsewhere.

### Amendment (recorded during step 2)

`command_parser.py` lives under `capability/`, not `environment/`. The
original layout placed the parser in `environment/parsing.py`, but two
capability modules consume its products at runtime: `view.py` prepares
mutations for a parsed command, and `tools/grammar.py` enriches one. Keeping
the parser in `environment/` would have made `capability/` import
`environment/` while `environment/` imported `capability.tools.facts` — a
bidirectional package dependency, exactly what the invariant exists to
prevent.

The parser is pure command-language knowledge (immutable data + pure
functions, no Session or Workspace state), so it belongs to the lower layer.
With it under `capability/`, the runtime import graph is strictly one-way:
`environment → capability → leaf modules`, verified mechanically by grep over
`capability/`'s imports.

## Migration sequence

Each step is independently committable and leaves the test suite green.
Ordering moves leaves before roots so no module is reshaped twice.

| Step | Content | Character |
|---|---|---|
| 1 | Move `ToolCommand`/`ToolReference` into `capability/tools/facts.py`; unify the three near-duplicate provenance dataclasses (`_ToolEntry`, `ToolReference`, `_CapabilityInspection`) onto one shared record with per-context additions | pure move, lowest risk |
| 2 | Create `capability/`; move the Workspace-lifetime modules (`_workspace.py`, `_capability_view.py`, `_tool_catalog.py`, `_tool_commands.py`, `_tool_environment.py`, `_tool_worker.py`) plus `_environment/command_parser.py` (see amendment); replace the worker path computation in `drivers/tool.py` (`Path(__file__).parents[2]`) with `importlib.resources.files` | mechanical move |
| 3 | Subtract inside `environment/` (and the moved parser): flatten `commands/` into one `commands.py`; delete `drivers/custom.py` (router calls `spec.prepare` directly); delete the `CommandParser` ABC and the `parse_shell_command` wrapper in `capability/command_parser.py` (one plain function); delete the `_route_decision` pass-through | pure subtraction |
| 4 | Split the Kernel God Class into `kernel.py` (Session state aggregate), `protocol.py` (syscall validation/snapshots), `supervisor.py` (Driver supervision + approval coordination) | structural, test-backed |
| 5 | Rename `_builtin_tools.py` → `_syscalls.py`; narrow `runtime/__init__.py` to the true Host surface; unify the mutator fact source (`_DIRECT_MUTATORS` vs `_DEFAULT_ASKED_EXECUTABLES`, including the duplicated in-place-`sed` detectors); remove defensive checks and single-use constants per `AGENTS.override.md` | finishing pass |

Step 5's public-surface narrowing: `runtime/__init__.py` keeps
`AgentRuntime`, `RuntimeClosedError`, the `model.py` message/event/Provider
types, the Host injection points (`ExecutionPolicy`, `ExecutablePolicy`,
`ExecutionApprover`, `ExecutionApprovalRequest`, `ApprovalResponse`,
`PolicyAction`), and the two Providers. Control-plane internals
(`CommandParseResult`, `ToolCommand`, `ToolReference`, `PolicyEvaluation`,
`ToolSchema`) stop being re-exported; tests import internal modules directly.
