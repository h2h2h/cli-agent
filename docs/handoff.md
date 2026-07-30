# Session handoff

Updated: 2026-07-30

## Repository state

- Milestones 01 through 05 are present on branch `v2`; the current head is
  `09e9662` (`feat(runtime): establish session-scoped environment kernel`).
- The revised milestone 06 Host-mediated execution approval implementation is
  in the working tree and awaits peer review. No commit was created.
- [RFC-0001](./rfcs/approved/RFC-0001-host-mediated-execution-approval.md)
  supersedes the adjacent scratch milestone 06 ticket for this repository; the
  external scratch artifact remains historical input rather than the active
  implementation contract.
- The working tree has 190 passing tests, and Ruff passes for `src` and
  `tests`.

## Implemented runtime

- The model-visible environment surface is fixed at `exec`, `output`, and
  `kill`. The control path is Parser → Policy Evaluation → optional Host
  Approval → final Execution Decision → Router → Scheduler → Driver. Only a
  final allow-only immutable `ExecutionDecision` reaches admission.
- Host Policy now evaluates `ALLOW`, `DENY`, or `ASK`. The public
  `ExecutablePolicy` supports disjoint executable basename sets and a default
  action. The built-in Policy asks for recognized direct `chmod`, `chown`,
  `cp`, `dd`, `install`, `ln`, `mkdir`, `mv`, `patch`, `rm`, `rmdir`, `tee`,
  `touch`, `truncate`, and `unlink`, and otherwise allows.
- `ASK` uses one bounded Runtime-wide asynchronous approver with an eight-call
  default active capacity and 60-second timeout. Missing approvers, timeout,
  callback failure, invalid responses, capacity exhaustion, and Host denial
  fail closed without creating an Execution or consuming Scheduler capacity.
  Session close cancels only that Session Kernel's pending approvals.
- The Reference CLI owns an allow-once prompt over its injected input and
  diagnostic streams. The headless Runtime owns no terminal UI or persistent
  approval choice.
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
  Policy and optional approval produce a final Decision for the exact parse
  result, the Router prefers a Runtime-owned Custom registry and otherwise
  selects the Shell Driver. Process creation is private to a Driver or handler
  and is not a routing category.
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
  basename. Executable allow/deny/ask lists are admission guardrails, not
  comprehensive side-effect detection or an operating-system sandbox.
- Ordinary Shell writes have no optimistic version check. Human approval
  decides whether one exact command may start; it neither detects stale reads
  nor makes concurrent file updates conflict-safe.
- Persistent Session `unset` and shell expansion for structured export do not
  exist. `tools` remains unregistered until the Capability View and Workspace
  Tool Environment are implemented.
- Execution States are in memory only and are not restored after Runtime
  restart.

## Next: milestone 07 — Mount the Capability View

After peer review of revised milestone 06, implement
`../.scratch/cli-agent-runtime/issues/07-mount-the-capability-view.md`.

Before code, amend that milestone's design to choose how ordinary CLI writes
interact with the Repertoire lower and Workspace upper now that there is no
Agent-visible managed write grammar. In particular, copy-up, whiteouts,
generated indexes, actual-layer provenance, and invalid authoritative
Workspace overrides need a concrete cross-platform filesystem strategy.

Keep Runtime-owned reconciliation and generated-file mutations separate from
ordinary Agent Shell writes: private Runtime operations still require Managed
Paths, atomic replacement, and trusted provenance even though they are not
exposed as `workspace write` commands.
