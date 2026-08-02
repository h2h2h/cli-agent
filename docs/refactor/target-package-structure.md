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

The command-routing and execution portions of this earlier target sketch are
superseded by [RFC-0007](../rfcs/proposed/RFC-0007-unified-command-routing-and-execution-refactor.md).
The current package uses `handlers/`, a Custom-first Router, and one global
Scheduler barrier model.

The layout borrows AEP's packaging principle — capability knowledge
(Workspace lifetime, shared across Sessions) is packaged separately from
Session machinery — without adopting AEP's internals. cli-agent's control
plane (Parser → Policy → Approval → Decision → Router → Scheduler → Handler),
its JSON-payload Tool worker, and its bounded output contracts are strictly
stronger than AEP's equivalents and are preserved as-is.

## Naming story

Two domains together form the platform presented to the model: a
directory-based environment through which the Agent uniformly discovers and
invokes capabilities (tools, skills, library).

- **`_capability/` is the content of the environment.** The capabilities
  themselves, organized by directory; the Capability View that mounts them;
  and the `.workspace` bootstrap that carries all of it. It answers "what can
  the model discover and invoke". Everything inside is reconciled once at
  Runtime open and shared by all Sessions.
- **`_environment/` is the machinery of the environment.** The Session-scoped
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
    ├── _capability/                # Workspace-lifetime capability domain
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
    └── _environment/            # Session-lifetime Environment Kernel domain
        ├── kernel.py              # Session state aggregate + control plane
        ├── protocol.py            # syscall argument validation and result
        │                          #   payload shapes (incl. _snapshot)
        ├── supervisor.py          # Execution supervision: scheduling,
        │                          #   Handler lifecycle, cancellation
        ├── policy.py  routing.py  scheduler.py  execution_state.py
        ├── handlers/              # command preparation and execution seam
        │   ├── base.py  shell.py  tools.py  executions.py
        │   ├── cd.py  export.py
        └── commands.py            # registry + builtins flattened into one
```

## Component roles

### `_capability/`

Workspace-scoped capability knowledge. Reconciled once at Runtime open;
Sessions receive references, never copies to mutate. Owns provenance,
validation, whiteouts, generated indexes, and the Tool dependency venv.
`tools/facts.py` is a pure-data leaf module: it imports nothing from
`_environment/`, which breaks the potential import cycle between
`grammar.py` (which consumes `CommandParseResult`) and the Session command
machinery. Tool grammar facts are created only inside the capability Tool
handler; the generic parser has no Tool-specific field.

### `_environment/`

The Session-scoped Kernel domain. One Kernel per Session, owning cwd, the
Session environment copy, the Scheduler, the Handle namespace, Execution
States, and Command Handler lifecycle.

### `_environment/commands.py` — Runtime-owned command dispatch table

Answers "who defines this command's behavior", *before* routing. A registry
mapping an exact command head to a Runtime-trusted `_CustomCommand`
(`prepare` + scheduling rule), consulted before the Shell fallback.

It exists because some commands cannot be delegated to a child Shell: `cd`
and `export` must mutate the Session's own cwd and environment table, and
child-process state changes cannot propagate back to the parent. Their
`prepare` receives the `_CommandContext` hooks (`set_cwd`, the Session env
mapping) and performs the mutation in-process.

It knows nothing about policy, approval, scheduling, or output bounds. It
grows along the *command vocabulary* dimension: a new Runtime-owned command
(e.g. `unset`) is one new registry entry.

### `_environment/handlers/` — command execution seam

Answers "how an admitted Decision actually runs", *after* routing. The only
place that performs side effects, consuming immutable Execution Decisions
without re-parsing or re-authorizing.

- `base.py`: contracts — `_CommandContext`, `_PreparedExecution`, and
  `_ExecutionOutcome`.
- `executions.py`: reusable execution primitives — `_InlineExecution`
  (in-process cooperative handler) and `_ProcessExecution` (child process,
  process group, stream capture, grace-period cancellation).
- `shell.py`: Shell command family; prepares the child Shell execution.
- `tools.py`: Tool command family; list/info use inline text executions, and
  run assembles the JSON payload and spawns the venv worker.
- `cd.py` and `export.py`: Session-mutating inline command handlers.

Custom commands from `commands.py` also produce `_InlineExecution`: both
components feed the same `_PreparedExecution` contract, so the supervisor
applies one backend-neutral wait/output/cursor/kill/cleanup discipline with
no command-family branches in the Kernel.

This seam grows along the *command handler* dimension: future MCP behavior
enters the Tool handler and Catalog without adding a model-visible syscall or
restoring a Tool-specific route.

### `_syscalls.py`

The fixed model-visible syscall schemas (`exec`, `output`, `kill`). Renamed
from `_builtin_tools.py`: "built-in tool" collides with the built-in custom
commands (`cd`, `export`), and CONTEXT.md already names this concept "AEP
Syscall".

## Dependency invariants

The refactor is complete when these hold and are enforced by review:

```
cli layer       → runtime public surface
_loop           → _environment + model
_environment    → _capability (one-way; _capability never imports _environment)
providers       → model
_capability/tools/facts.py      (imports nothing outside stdlib/typing)
```

The test for a misplaced module: if anything under `_capability/` reaches for
a Session concept, it belongs elsewhere.

## Module naming

The underscore prefix marks "not part of the public API; direct imports are
unsupported", following the httpx/pydantic/sklearn convention:

- Public-backing modules at the `runtime/` root stay bare: `runtime.py`,
  `model.py`, `providers/`.
- Internal modules at the `runtime/` root carry the prefix: `_loop.py`,
  `_system_message.py`, `_syscalls.py`.
- Internal subpackages carry the prefix: `_environment/`, `_capability/`.
- Modules inside an underscored package stay bare; the package prefix
  already carries the signal (e.g. `_environment/kernel.py`,
  `_capability/tools/facts.py`).

A symbol may be public while living in an underscored module (e.g.
`SyscallSchema` in `_syscalls.py`): the prefix constrains the module, not the
symbol, which Hosts import from `cli_agent.runtime`.

### Amendment (recorded during step 2)

`command_parser.py` lives under `_capability/`, not `_environment/`. The
original layout placed the parser in `_environment/parsing.py`, but two
capability modules consume its products at runtime: `view.py` prepares
mutations for a parsed command, and `tools/grammar.py` enriches one. Keeping
the parser in `_environment/` would have made `_capability/` import
`_environment/` while `_environment/` imported `_capability.tools.facts` — a
bidirectional package dependency, exactly what the invariant exists to
prevent.

The parser is pure command-language knowledge (immutable data + pure
functions, no Session or Workspace state), so it belongs to the lower layer.
With it under `_capability/`, the runtime import graph is strictly one-way:
`_environment → _capability → leaf modules`, verified mechanically by grep over
`_capability/`'s imports.

### Amendment (recorded during step 4)

Two placements differ from the original split sketch:

- **Approval stays in the Kernel.** `_authorize` consumes a PolicyEvaluation
  and produces an ExecutionDecision without touching the Scheduler, the
  Execution registry, or Handlers — it is control-plane, not supervision. The
  `_approval_tasks` set exists only so `close()` can cancel in-flight
  approvals, so it follows the Kernel lifecycle.
- **`_snapshot` moved to `protocol.py`.** Its only callers assemble syscall
  result payloads, and its output shape mirrors the
  `_EXECUTION_OUTPUT_SCHEMA` in `_builtin_tools.py`; the protocol module owns
  every model-visible payload shape.

The supervisor reaches Session state (workspace, cwd, env, the Execution
registry) through a `session` back-reference to its Kernel, annotated under
`TYPE_CHECKING`. The Kernel remains the single owner of Session state, which
keeps the white-box tests (`kernel._executions`, `kernel._env`) valid.

## Migration sequence

Each step is independently committable and leaves the test suite green.
Ordering moves leaves before roots so no module is reshaped twice.

| Step | Content | Character |
|---|---|---|
| 1 | Move `ToolCommand`/`ToolReference` into `_capability/tools/facts.py`; unify the three near-duplicate provenance dataclasses (`_ToolEntry`, `ToolReference`, `_CapabilityInspection`) onto one shared record with per-context additions | pure move, lowest risk |
| 2 | Create `_capability/`; move the Workspace-lifetime modules (`_workspace.py`, `_capability_view.py`, `_tool_catalog.py`, `_tool_commands.py`, `_tool_environment.py`, `_tool_worker.py`) plus `_environment/command_parser.py` (see amendment); replace the worker path computation in `handlers/tools.py` with `importlib.resources.files` | mechanical move |
| 3 | Subtract inside `_environment/` (and the moved parser): flatten `commands/` into one `commands.py`; route Custom commands directly through the registry; delete the `CommandParser` ABC and the `parse_shell_command` wrapper in `_capability/command_parser.py` (one plain function); delete the `_route_decision` pass-through | pure subtraction |
| 4 | Split the Kernel God Class into `kernel.py` (Session state aggregate), `protocol.py` (syscall validation/snapshots), `supervisor.py` (Handler supervision + approval coordination) | structural, test-backed |
| 5 | Rename `_builtin_tools.py` → `_syscalls.py`; narrow `runtime/__init__.py` to the true Host surface; unify the mutator fact source (`_DIRECT_MUTATORS` vs `_DEFAULT_ASKED_EXECUTABLES`, including the duplicated in-place-`sed` detectors); remove defensive checks and single-use constants per `AGENTS.override.md` | finishing pass |

Step 5's public-surface narrowing: `runtime/__init__.py` keeps
`AgentRuntime`, `RuntimeClosedError`, the `model.py` message/event/Provider
types, the Host injection points (`ExecutionPolicy`, `ExecutablePolicy`,
`ExecutionApprover`, `ExecutionApprovalRequest`, `ApprovalResponse`,
`PolicyAction`), and the two Providers. Control-plane internals
(`CommandParseResult`, `ToolCommand`, `ToolReference`, `PolicyEvaluation`,
`ToolSchema`) stop being re-exported; tests import internal modules directly.

### Amendment (recorded during step 5)

The public-surface audit reversed the narrowing plan. Every type appearing in
the signature of a Host-implemented interface is public by definition:
`ExecutionPolicy.evaluate` receives a `CommandParseResult` (whose `tool`
field exposes `ToolCommand`) and returns a `PolicyEvaluation`, and a custom
`ModelProvider` receives a `ModelRequest` carrying `SyscallSchema` entries.
The export list therefore maps exactly onto the three Host seams (Runtime
lifecycle, model, policy) and no members were removed. The only change is
terminology: `ToolSchema` → `SyscallSchema` and
`BUILDIN_TOOL_SCHEMA_DEFINITIONS` → `BUILT_IN_SYSCALL_SCHEMAS` (also fixing
the `BUILDIN` typo), with `_builtin_tools.py` renamed to `_syscalls.py`.

Also completed under this step:

- The recognized-mutator fact source was unified in
  `_capability/command_parser.py`: one `_DIRECT_MUTATORS` table (fifteen
  names, `sed` excluded) and one `_sed_is_in_place` helper now feed both the
  default Policy's asked set and the Capability View's mutation preparation.
  The pre-existing behavioral difference on `sed` was already reconciled by
  both sides gating on in-place detection, so the unification changes no
  behavior.
- Defensive checks re-verifying invariants already guaranteed by their own
  constructors were removed per `AGENTS.override.md`: the dotenv
  `O_NOFOLLOW`/`fstat` TOCTOU dance, `PolicyEvaluation.__post_init__`,
  `ExecutionDecision.__post_init__`, and the Kernel constructor's duplicate
  workspace/limit validations. Checks on genuinely external input (Host
  configuration, Provider-reported usage, dotenv file format) remain.
- Single-use module constants were inlined; constants shared across
  signatures or call sites (`_DEFAULT_QUEUE_LIMIT`, `_DEFAULT_PARALLEL_LIMIT`,
  `_CAPABILITY_DIRECTORIES`, `_EXECUTION_OUTPUT_SCHEMA`, the Tool environment
  state file names) were kept. The triplicated `_ensure_real_directory` and
  duplicated `_atomic_write` helpers converged into
  `_capability/workspace.py`.
