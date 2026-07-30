# Session handoff

Updated: 2026-07-30

## Repository state

- Milestones 01 through 04 are committed on branch `v2`. Milestone 05 is
  implemented in the working tree and awaits peer review; no commit was
  created. The current head remains `c0b8e84` (`docs: update handoff.md`).
- The working tree now has 153 passing tests. The seven
  `pending_execution_capacity` validation failures recorded previously are
  fixed, and Ruff passes for `src` and `tests`.

## Implemented runtime

- The model-visible environment surface is fixed at `exec`, `output`, and `kill`.
  The control path is Parser → Policy → Router → Scheduler → Driver; only an
  allowed immutable `ExecutionDecision` reaches admission.
- Long-running Shell Executions expose Session-private Handles, bounded
  incremental output, stable Cursors, cancellation, and process-group cleanup.
- Every Session owns a bounded ordered Scheduler with default pending capacity
  32. Commands remain serial by default; consecutive Runtime-trusted
  `PARALLEL_SAFE` commands may run in bounded batches without crossing an
  earlier serial barrier. Sessions run concurrently, policy denial consumes no
  capacity, and full pending admission returns `queue_full`.
- Handles and cleanup remain Session-private. Close releases queued and running
  work; later reuse of the same Session ID creates fresh transient state.
- Runtime open now resolves the existing Workspace and idempotently creates or
  validates the `.workspace` directory and `.workspace/env` regular dotenv file
  before any Session Kernel construction. Wrong object types and symbolic
  links fail open; close and later open failure preserve the persistent
  namespace.
- Runtime open parses the strict UTF-8 dotenv file with `python-dotenv` into an
  immutable Runtime-owned snapshot before Session Kernel construction.
  Later file changes affect only a later Runtime open.
- Every active Runtime Session owns exactly one `AgentLoop` and one
  Session-scoped `EnvironmentKernel`. The Kernel owns an independent mutable
  copy of the Runtime-open environment snapshot; the copy survives turns, is
  isolated from other Sessions, and is cleared on close. Recreating a
  Host-visible Session ID creates a fresh Loop and Kernel.
- AEP-style Custom command dispatch now resolves registered command heads
  before falling back to Shell. `cd` and `export` are serial Custom handlers;
  `cd` persists Session cwd and `export` persists the Session environment.
  Both pass through Policy, admission, Execution snapshots, cancellation, and
  cleanup. Malformed export assignment sets fail atomically.
- Command parsing records only generic syntax facts. After the basename-only
  Policy allows the exact parse result, the Router prefers a Runtime-owned
  Custom registry and otherwise selects the Shell Driver. Process creation is
  private to a Driver or handler and is not a routing category.
- The Kernel consumes one Driver Execution `run`/`cancel` contract and one
  bounded output sink. `_ExecutionState` stores backend-neutral lifecycle
  state rather than subprocess-specific handles, so a future Tool Driver will
  not require Driver-type branches in Kernel supervision.
- Each Shell child explicitly receives `dict(os.environ) | session_env` at
  process start. Session values win collisions, later Host changes affect later
  Executions, and Provider credentials are intentionally inherited.
- `EnvironmentBinding`, hidden Kernel Session IDs, the Kernel Session registry,
  and the separate `EnvironmentSession` object are removed. `AgentLoop`
  dispatches directly to its Session Kernel. `kernel.py` owns Session state,
  lifecycle, and Driver supervision; protocol, policy, routing, scheduling,
  Execution state, and Drivers remain focused private implementation modules.
  See
  [Session-scoped Environment Kernel](./discussions/session-scoped-environment-kernel.md).

## Known limits

- Complete Host environment inheritance is deliberately not a Secret boundary.
  Agent commands can inspect or emit Provider credentials and other inherited
  values; `.workspace/env` is ordinary Agent-readable Workspace data.
- The dotenv loader deliberately disables interpolation and rejects bare keys.
  There is no automatic migration from the earlier experimental
  `.workspace/env/KEY` directory layout.
- The command inspector uses POSIX `shlex` and checks only the first token's
  basename. The deny policy is an admission guardrail, not comprehensive
  side-effect detection or an operating-system sandbox.
- Persistent Session `unset` and shell expansion for structured export do not
  exist. `tools` remains unregistered until the Capability View and Workspace
  Tool Environment are implemented.
- Execution States are in memory only and are not restored after Runtime
  restart.

## Next: milestone 06 — Make Workspace mutations conflict-safe

Implement
`../.scratch/cli-agent-runtime/issues/06-make-workspace-mutations-conflict-safe.md`.
The milestone introduces a collision-resistant reserved CLI grammar for
versioned managed reads, conditional atomic writes, and conditional removes.
The Control Plane must prove Managed Paths and authorize structured
`workspace.read`, `workspace.write`, and `workspace.remove` facts before a
private managed Driver acts.

Keep the distinction explicit: these optimistic managed operations detect
version conflicts, while arbitrary Shell commands can still write the
Workspace without participating in that protocol. Absolute paths, traversal,
symbolic-link escapes, unsupported composition, and stale versions must fail
without changing the target. The model-visible Tool surface remains `exec`,
`output`, and `kill`.
