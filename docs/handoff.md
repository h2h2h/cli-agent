# Session handoff

Updated: 2026-07-28

## Repository state

- Milestones 01 through 04 are complete on branch `v2`. The current head is
  `ed580d6` (`refactor(environment): split kernel responsibilities`).
- The full suite has 121 passing tests. Ruff lint, Ruff format, and whitespace
  checks pass.

## Implemented runtime

- The model-visible environment surface is fixed at `exec`, `output`, and `kill`.
  The control path is Parser → Policy → Router → Scheduler → Driver; only an
  allowed immutable `ExecutionDecision` reaches admission.
- Long-running Shell Executions expose Session-private Handles, bounded
  incremental output, stable Cursors, cancellation, and process-group cleanup.
- Every Session owns a bounded Scheduler with default pending capacity 32 and a
  serial FIFO Shell lane. Sessions run concurrently; policy denial consumes no
  capacity and full pending admission returns `queue_full`.
- Handles and cleanup remain Session-private. Close releases queued and running
  work; later reuse of the same Session ID creates fresh transient state.
- `kernel.py` now retains lifecycle and linear orchestration. Protocol, policy,
  routing, scheduling, supervision, Execution state, and the Shell Driver live
  in focused private modules under `_environment/`.

## Known limits

- Shell children currently inherit the embedding process's complete
  environment. This can expose direnv-loaded Provider credentials and is the
  primary gap for milestone 05.
- The command inspector uses POSIX `shlex` and checks only the first token's
  basename. The deny policy is an admission guardrail, not comprehensive
  side-effect detection or an operating-system sandbox.
- Persistent Session `cd` and `export` do not exist. Cwd/environment generations
  and their state barrier remain deferred until cross-Execution mutation is
  introduced.
- Execution records are in memory only and are not restored after Runtime
  restart.

## Next: milestone 05 — Filter the Workspace environment

First decompose the source ticket at
`../.scratch/cli-agent-runtime/issues/05-filter-the-workspace-environment.md`
under `docs/issues/05-filter-the-workspace-environment/`.

The milestone must establish the following contract:

1. Persist an Agent-visible Workspace Environment Request containing variable
   names only, and accept an explicit name-to-value Host Environment Grant at
   Runtime open.
2. Snapshot the intersection of request and grant plus a Runtime-controlled
   minimum environment. Never treat the embedding process's complete
   `os.environ` as an implicit grant.
3. Give each Session a fixed filtered environment snapshot. Changes to the
   request, grant mapping, or embedding environment affect only a later Runtime
   open.
4. Make every `CommandParseResult` reference the exact effective environment
   authorized for that Session, and make the Shell Driver pass it explicitly
   to the child process instead of reconstructing it from `os.environ`.
5. Keep environment values out of Workspace files, capability files, indexes,
   generated Tools, policy facts, denial messages, and Runtime diagnostics.
   Model Provider credentials remain internal unless explicitly requested and
   granted for Agent-executed work.

Likely implementation seams are `AgentRuntime.open` for the Host Grant,
Workspace-open logic for the names-only request and effective snapshot,
`CommandParseResult` for the immutable environment reference, Session creation
for snapshot ownership, and `drivers/shell.py` for explicit child injection.
