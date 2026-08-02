# Session handoff

Updated: 2026-08-02

## Repository state

- The working tree is on branch `refactor`. Milestone 10 Tool capability
  commands and the capability-package refactors are committed; milestone 08
  (Discover and load Skills on demand) is complete on top of them.
- Milestone 13 (Project MCP tools during Runtime open) is implemented: issue 01
  (MCP dependency and config facts), 02 (diagnostic seam), 03 (reconcile by
  full rebuild), 05 (Runtime base dependency injection), and 06 (prove
  projection and invocation) are committed; issue 04 (MCP provenance) was
  cancelled by decision.
- Milestone 08 commits: `9a62292` (strictyaml + Skill entry facts),
  `3c43379` (SKILL.md frontmatter parse and validate),
  `b9cc904` (Skill Catalog and generated index), and
  `4952e1e` (compact Skill catalog in model context).
- [RFC-0001](./rfcs/approved/RFC-0001-host-mediated-execution-approval.md)
  supersedes the adjacent scratch milestone 06 ticket for this repository; the
  external scratch artifact remains historical input rather than the active
  implementation contract.
- [RFC-0002](./rfcs/approved/RFC-0002-workspace-capability-view.md) supersedes
  the original M7 filesystem strategy for this repository.
- [RFC-0003](./rfcs/approved/RFC-0003-tool-capability-commands.md) records the
  implemented M10 command grammar, Catalog, Tool Environment, worker, Policy,
  and scheduling decisions.
- [RFC-0004](./rfcs/proposed/RFC-0004-skill-discovery-and-loading.md) is
  PROPOSED; the milestone 08 tickets live in
  `docs/issues/06-discover-and-load-skills-on-demand/`.
- [RFC-0006](./rfcs/proposed/RFC-0006-explicit-runtime-resource-ownership.md)
  is PROPOSED; the milestone 15 tickets live in
  `docs/issues/15-make-runtime-resource-ownership-explicit/`.
- Milestone 15 (Make Runtime resource ownership explicit) is implemented:
  issue 01 introduced the `_RuntimeResources` aggregate and
  `_reconcile_runtime_resources()`, issue 02 migrated `AgentRuntime` to own a
  single aggregate, and issue 03 added ownership-boundary tests and updated
  the architecture diagram.
- The working tree has 326 passing tests, and Ruff passes for `src` and
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
- Runtime open builds a trusted `_SkillCatalog` from top-level effective
  `.workspace/skills/*` directories using the same Capability View provenance
  and whiteout rules as Tools. Entries record name, path, provenance, shadow
  facts, structural validity, and validation error without raising.
- SKILL.md frontmatter is parsed with `strictyaml`. `name` (lowercase,
  letters/digits/hyphens, ≤64, must equal the directory name) and `description`
  (≤1024) are required and strict; `license`, `allowed-tools`, and
  `compatibility` are optional string checks only. Missing, unclosed, or
  non-mapping frontmatter is reported as an entry error.
- `skills/index.md` is an atomically generated, reproducible, non-authoritative
  projection of the compact catalog. It is regenerated on every Runtime open
  and never trusts an authored index.
- The runtime-assembled system message appends a compact Skills section listing
  each discovered Skill by name, status, and summary only — never the full
  SKILL.md body. Full instructions remain available on demand through ordinary
  environment access, for example `cat .workspace/skills/<name>/SKILL.md`.
- Adding Skills adds no model Tool and no reserved command head. The
  model-visible surface stays exactly `exec`, `output`, and `kill`, and the
  Runtime public exports are unchanged.
- `_mcp` is a mounted Capability View configuration directory (like
  `tools`/`skills`/`library`): Repertoire `_mcp/<server>/config.json`
  descriptions appear as exact lower links under `.workspace/_mcp/`, real
  Workspace files override them, and a whiteout disables a server. Runtime
  open projects the effective descriptions into generated Tool stubs at
  `.workspace/tools/mcp_<server>.py` by full rebuild: stale `mcp_*` stubs are
  removed first, servers are discovered in parallel, and only successfully
  discovered servers produce a stub. The `mcp_` filename prefix is the sole
  ownership basis; hand-authored Tools without it are never touched. The
  `_mcp` config never appears in the tools/skills index and stores only env
  variable names, never values.
- MCP discovery is fail-to-none: a missing or structurally invalid config, or
  a server that exhausts discovery retries, emits a non-blocking Runtime
  Diagnostic and produces no stub, and a previous stub for that server is
  removed on the next reconcile. A whiteouted server is disabled without a
  diagnostic. Workspace open never blocks on MCP projection.
- The Tool worker venv resolves user `tools/requirements.txt` plus the
  Runtime-owned `mcp` base dependency (deduped), and synchronization compiles
  a lockfile before `uv pip sync` so transitive dependencies are installed.
  A generated MCP stub runs through the ordinary `tools run` surface, mixes
  with local Tools in one code block, and a connection failure returns an
  ordinary failed Tool Result without deleting the stub.
- MCP projection adds no model Tool and no reserved command head. The
  model-visible surface stays exactly `exec`, `output`, and `kill`, the
  Runtime public exports are unchanged, and additional Sessions do not re-run
  reconcile.
- Runtime-owned Workspace state converges into one `_RuntimeResources`
  aggregate (`runtime/_resources.py`): Workspace root, immutable base
  environment snapshot (`repr=False`), Capability View, Tool Catalog, Tool
  Environment, and Skill Catalog. `_reconcile_runtime_resources()` keeps the
  established open order and the reconcilers' exception, fail-soft, atomic
  write, and persistent-state semantics. `AgentRuntime` no longer saves any
  parallel Workspace field, and the MCP projection result is not retained;
  only its generated Tool files are consumed by the Tool Catalog.
- Sessions borrow only the explicit objects they need. `EnvironmentKernel` and
  the system message assembler select fields from the aggregate by name and
  never receive the full aggregate, so `_environment` does not depend on the
  Runtime composition type. Each Kernel still copies the immutable base
  environment snapshot into its own mutable Session environment. Host-owned
  Provider, Policy, Approver gate, and diagnostic callback stay outside the
  aggregate and are never closed by `close_session()` or Runtime close, which
  still only close Session-owned Kernel state.

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
- Skill discovery is advertisement only: the compact model context lists name,
  status, and summary, and the model must read full instructions on demand.
  There is no Skill-specific Tool or reserved command, and the `skills`
  directory holds no executable contract by itself.
- Skill frontmatter validation is structural, not safety certification. The
  `license`, `allowed-tools`, and `compatibility` fields receive only string
  type checks, and full SKILL.md instructions are not evaluated or trusted by
  the Runtime.
- The Skill Catalog and generated index are Runtime-open snapshots. Skill files
  created or changed during an active Runtime are reconciled on the next open.
- MCP stubs are pure generated artifacts of Runtime open and are fully rebuilt
  (including overwriting local edits) on the next open. A server that becomes
  unreachable at invocation time keeps its stub until the next reconcile.
  MCP description changes under Repertoire take effect on the next open;
  real Workspace `_mcp` files are visible to the next reconcile without
  re-opening.
- In M13 a worker venv needs the Runtime-owned `mcp` package because a stub
  connects directly to its server. M14 will remove that need when stubs switch
  to an IPC shim.

## Next

Peer review milestone 15 (issues 01, 02, and 03) and RFC-0006, then commit only
on explicit request. The next milestone is 14 — Invoke MCP tools with bounded
bindings — where worker stubs switch to an IPC shim so the worker no longer
needs `mcp`, and the projection contract gains bounded bindings.
