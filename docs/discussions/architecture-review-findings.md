# Architecture review findings

Status: draft
Recorded: 2026-07-30

Preliminary findings from an end-to-end read of `src/cli_agent` (~5.3 KLOC source,
~9.8 KLOC tests). The goal is to surface refactor candidates, not to ratify a
plan. Each finding names a concrete site, the cost it imposes today, and the
rough shape of a fix.

Overall: the architecture is sound. The user-facing layer
(`cli/runner/presentation/config`), the Runtime lifecycle (`runtime.py`), the
model seam (`model.py` + `providers/` + `_agent_loop.py`), and the Session
kernel (`runtime/_environment/*`) are separated, and the documented control
path Parser -> Policy -> Approval -> Decision -> Router -> Scheduler -> Driver
matches the code. Tests cover the surface densely. The issues below are about
abstraction overhead, a God Class, and duplicated semantics, not about
foundational structure.

## 1. The `_AgentRuntimeOpener` indirection is not earning its keep

`AgentRuntime.open` is a `@classmethod` that returns a `_AgentRuntimeOpener`,
which then forwards fifteen constructor parameters through
`__init__` -> `_open` -> `AgentRuntime._open` -> `AgentRuntime.__init__`
(see `src/cli_agent/runtime/runtime.py:89` and `:290`).

The only benefit is supporting both `await AgentRuntime.open(...)` and
`async with AgentRuntime.open(...)` from the same call site. The same
ergonomics are available from a plain `async def open(...)` classmethod that
returns an already-constructed `AgentRuntime`, paired with the existing
`__aenter__/__aexit__` on `AgentRuntime` itself. The opener state machine
(`_runtime: AgentRuntime | None`, the `_ensure_open` re-entry branch, the
mirrored `__aenter__/__aexit__`) is overhead with no second caller.

## 2. Single-implementation abstractions in the command path

`CommandParser` is an `ABC` with exactly one implementation,
`ShlexCommandParser`, plus a top-level convenience function
`parse_shell_command` (`runtime/_environment/command_parser.py:50`). Only one
call site in the codebase invokes `parser.parse(...)`. The polymorphism is
speculative. A single function returning `CommandParseResult` would remove
the ABC, the subclass, and the convenience wrapper in one cut.

The Driver seam is similar but justified: `_DriverExecution` and
`_ExecutionDriver` as Protocols back the inline vs. process vs. tool
distinction, which is real. They stay.

## 3. `_CustomDriver` and `_ResolvedCustomDriver` add a layer without behavior

`_CustomDriver.resolve(command)` returns a `_CustomCommandSpec`;
`_CustomDriver.bind(spec)` wraps it in a `_ResolvedCustomDriver`, whose
`prepare` is `return self._spec.prepare(command, context)`
(`runtime/_environment/drivers/custom.py:33`). The intermediate object holds
no state, performs no computation, and introduces no indirection that the
caller needs. `_CommandRouter.route` can call `spec.prepare` directly once a
spec is resolved.

## 4. `EnvironmentKernel` is a God Class

`runtime/_environment/kernel.py` is 577 lines and mixes at least four
responsibilities inside one class:

- protocol/validation (`_apply_defaults`, `_validate_arguments`,
  `_SCHEMA_BY_NAME`, the `dispatch` switch over `exec/output/kill`);
- batch admission and ASK authorization (`dispatch_batch`, `_authorize`,
  `_await_initial_exec`, the `_approval_tasks` set);
- Driver supervision (`_admit`, `_start_execution`, `_run_execution`,
  `_terminate`, `_set_cwd`);
- Session state (cwd, env, `_executions`, close coordination with the
  Scheduler).

Each of these is internally cohesive and externally coupled only through
narrow surfaces. Splitting the protocol/validation helpers into a
`_tool_protocol` module and lifting the supervision loop into an
`_execution_supervisor` (still Session-scoped) would leave `Kernel` as the
small state aggregator the handoff already describes it as.

## 5. Package nesting is heavier than the code needs

`runtime/_environment/commands/` and `runtime/_environment/drivers/` are
subpackages whose entire contents are private and consist of one to three
small modules each. Every consumer import reads
`from cli_agent.runtime._environment.drivers.base import _DriverContext`
(`runtime/_environment/kernel.py:19`). At this scale, a flat layout
(`_environment/drivers.py`, `_environment/commands.py`, or merging the
contents back into `kernel.py` siblings) keeps the same privacy and removes
one path component from every import.

## 6. Cross-module semantic duplication

Three duplications are visible today and each carries a consistency risk:

- The "direct filesystem mutator" set is maintained in two places:
  `runtime/_capability_view.py:_DIRECT_MUTATORS` and
  `runtime/_environment/policy.py:_DEFAULT_ASKED_EXECUTABLES`. The two sets
  disagree on `sed`, and the disagreement is patched over by separate
  in-place-`sed` detectors implemented in both modules.
- Three near-identical provenance dataclasses exist: `_ToolEntry`
  (`_tool_catalog.py`), `ToolReference` (`_environment/command_parser.py`),
  and `_CapabilityInspection` (`_capability_view.py`). Their fields overlap
  almost completely (name, provenance, shadows_repertoire, valid,
  validation_error). The boundary between "a Tool file", "a Tool reference
  in a command", and "a Capability View path" is real, but it does not
  require three dataclasses; one shared record plus per-context additions
  would do.
- Four `_validate_*` helpers (`_validate_approval_capacity`,
  `_validate_approval_timeout`, `_validate_queue_limit`,
  `_validate_parallel_limit`) are the same `isinstance(int) and not bool and
  >= lower` pattern repeated across `policy.py` and `scheduler.py`.

## 7. Over-defensive boundaries and excess re-export

`runtime/__init__.py` re-exports roughly thirty internal symbols,
including `PolicyAction`, `PolicyEvaluation`, `CommandParseResult`,
`ToolCommand`, and similar protocol-level types. `runtime` is not a public
API; it has three local consumers (`cli.py`, `runner.py`,
`presentation.py`). Treating it as one flattens the boundary between
"types the Host sees" and "types the Runtime uses internally" and forces
every internal rename to also be a re-export edit.

The defensive code is a separate axis but listed under the same finding
because both stem from treating internal boundaries as load-bearing. Sites
that stand out:

- `runtime/_workspace.py:_load_workspace_environment` opens the dotenv file
  with `O_NOFOLLOW`, then re-`fstat`s the descriptor and compares
  `(st_dev, st_ino)` against the earlier `lstat`. The protection is against
  a TOCTOU swap of a Workspace-local file that the same user owns.
- `_ensure_real_directory` / `_ensure_real_file` are duplicated nearly
  verbatim between `_workspace.py` and `_capability_view.py`.
- `PolicyEvaluation.__post_init__`
  (`runtime/_environment/policy.py:57`) enforces invariants ("allow cannot
  have a reason", "deny/ask must have a reason") that the class's
  constructors already guarantee by their signatures. The runtime check
  duplicates the type-level discipline.

These checks also conflict with the project-level guidance recorded in
`src/cli_agent/AGENTS.override.md` (remove redundant boundary checks;
avoid excess global constants; avoid private nested classes), so resolving
this finding also satisfies a stated objective.

## Suggested ordering

The findings are largely independent. A minimal-risk sequence is:

1. Findings 1, 2, 3, 4-the-helper-split: pure simplification, no behavior
   change, individually testable.
2. Finding 6: unify the mutator set and the provenance dataclass; merge the
   `_validate_*` helpers.
3. Findings 4-the-supervisor-split and 5: reshape the Kernel and flatten the
   subpackages.
4. Finding 7: trim `runtime/__init__.py` to a true public surface and remove
   the over-defensive sites in the same pass, since both touch the same
   files.

Each step should leave the test suite green. No step is required to proceed
to the next; the ordering only minimizes cross-step rework.
