# Session handoff

Updated: 2026-07-30

## Repository state

- Milestones 01 through 07 are present on branch `v2`; the current head is
  `8c31741` (`feat(workspace): mount capability view`).
- The prioritized milestone 10 Tool capability command implementation is in
  the working tree and awaits peer review. No M10 commit was created.
- [RFC-0001](./rfcs/approved/RFC-0001-host-mediated-execution-approval.md)
  supersedes the adjacent scratch milestone 06 ticket for this repository; the
  external scratch artifact remains historical input rather than the active
  implementation contract.
- [RFC-0002](./rfcs/approved/RFC-0002-workspace-capability-view.md) supersedes
  the original M7 filesystem strategy for this repository.
- [RFC-0003](./rfcs/approved/RFC-0003-tool-capability-commands.md) records the
  implemented M10 command grammar, Catalog, Tool Environment, worker, Policy,
  and scheduling decisions.
- The working tree has 245 passing tests, and Ruff passes for `src` and
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
  32. Default and Tool lanes claim work independently while preserving FIFO
  admission within each lane. Commands remain serial within their lane by
  default; consecutive Runtime-trusted `PARALLEL_SAFE` commands may run in
  bounded batches. Sessions run concurrently, policy denial consumes no
  capacity, and full pending admission returns `queue_full`.
- Handles and cleanup remain Session-private. Close releases queued and running
  work; later reuse of the same Session ID creates fresh transient state.
- Runtime open now resolves the existing Workspace and idempotently creates or
  validates the `.workspace` directory and `.workspace/env` regular dotenv file
  before any Session Kernel construction. Wrong object types and symbolic
  links fail open; close and later open failure preserve the persistent
  namespace.
- Runtime open accepts an optional Repertoire and otherwise creates or
  validates `~/.config/cli-agent/repertoire/{tools,skills,library}`. The
  Reference CLI exposes the same selection through `--repertoire`.
- `.workspace/tools`, `.workspace/skills`, and `.workspace/library` are real
  directories forming the visible Capability View. Lower Repertoire files
  appear as exact file-level symbolic links; real Workspace entries take
  precedence, including structurally invalid overrides.
- Recognized approved writes copy lower-backed targets into the Workspace with
  atomic replacement before Shell spawn. Lower-only removal creates a
  persistent whiteout, removing an override immediately reveals lower, and
  removing a Workspace-only file leaves it absent.
- Private inspection reports Repertoire, Workspace, or whiteout provenance and
  shadow facts from actual layer state. Repertoire and conflicting Workspace
  symbolic links are rejected rather than trusted from authored metadata.
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
- Command parsing records generic syntax facts plus explicit file output
  redirection. After Policy and optional approval produce a final Decision for
  the exact parse result, the Router prefers a Runtime-owned Custom registry
  and otherwise selects the Shell Driver. The default Policy asks for direct
  known mutators, explicit output redirection, and in-place `sed`.
- Runtime open builds a trusted Tool Catalog from top-level effective
  `.workspace/tools/*.py` entries. It validates identifier names, Capability
  View provenance, UTF-8 source, and Python syntax without importing modules.
  `tools/index.md` is an atomically generated projection, not authority.
- The exact top-level `tools` command head is reserved. `tools list`,
  `tools info <name>`, quoted `tools run`, and exact `PY<< ... PY` blocks
  follow the Tool Driver; malformed composition does not fall back to Shell.
  Policy receives immutable operation, reference, validation, provenance, and
  dynamic-reference facts. The current default Policy allows every Tool
  operation as explicitly selected by the project owner.
- `.workspace/.tool-environment/.venv` is mutable state private to one
  Workspace. Runtime open hashes the effective `tools/requirements.txt` and
  uses `uv pip sync` only when it changes. Sync failures are fail-soft:
  list/info remain available while run reports the stored error without Host
  Python fallback.
- Every Tool run starts the private venv Python with one fixed stdlib-only
  worker and a JSON stdin payload. Only Catalog-valid modules are offered in
  the `tools` namespace. Fresh workers isolate module state, and the existing
  process Execution path supplies bounded output, Handles, cancellation, and
  process-group cleanup.
- Tool routes use a bounded lane independent of the default Shell/Custom lane.
  List/info are Runtime-trusted parallel-safe operations. Run is parallel-safe
  only when all statically referenced names are valid and present in the
  Host's `parallel_tools` allow list; dynamic or incomplete references remain
  serial. AgentLoop admits a model batch in returned order, waits concurrently,
  and writes Tool Results back in that same order.
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
- The command inspector uses POSIX `shlex`, direct executable basenames,
  explicit output redirection, and in-place `sed` flags. Wrappers, compound
  commands, scripts, interpreters, and runtime-computed paths remain outside
  comprehensive side-effect detection.
- The file-level link view is cooperative, not an operating-system sandbox.
  An unrecognized write may follow a lower link, and direct access to the
  external Repertoire path bypasses copy-up.
- Ordinary Shell writes have no optimistic version check. Human approval
  decides whether one exact command may start; it neither detects stale reads
  nor makes concurrent file updates conflict-safe.
- Persistent Session `unset` and shell expansion for structured export do not
  exist.
- Tool structural validation is not safety certification. The default Policy
  allows arbitrary Tool Python, workers inherit the effective child
  environment, and no filesystem, process, network, or Secret sandbox is
  added.
- Tool Catalog, generated index, and dependencies are Runtime-open snapshots.
  Tool files created or changed during an active Runtime are reconciled on the
  next open.
- Workspace provenance does not yet distinguish human-authored from
  Agent-authored Tool files. This distinction is not currently needed by the
  default-allow Tool Policy.
- Execution States are in memory only and are not restored after Runtime
  restart.

## Next

Peer review RFC-0003 and the M10 working tree, then commit only on explicit
request. After M10 is accepted, return to milestone 08 — Discover and load
Skills on demand — using the same actual-layer provenance and generated-index
rules.
