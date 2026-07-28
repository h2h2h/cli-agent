# Session handoff

Updated: 2026-07-28

## Completed

- Preserved the final v1 code state on `main` and the annotated
  `cli-agent-v1-baseline-2026-07-27` tag; `v2` remains the development branch.
- Accepted `docs/discussions/unified-execution-dispatch.md` and
  `docs/discussions/control-plane-and-execution-plane.md` for specification.
- Added architecture decision 16 in the parent AI-Coding repository and
  reconciled its amendments into decisions 04, 08, and 10, `CONTEXT.md`, both
  RFC-0001 languages, the architecture map, and an implementation issue impact
  matrix.
- Revised implementation milestones 03, 04, 05, 06, 10, and 15 around the
  private Execution Control Plane / Execution Plane boundary.
- Completed the milestone 03 Shell-only tracer bullet:
  - validated `exec` requests become Shell candidates without side effects;
  - the Runtime-lifetime Host policy runs before admission;
  - allowed candidates freeze into immutable `ExecutionPlan` values;
  - long-running Shell Executions expose running snapshots and stable
    incremental Cursor reads;
  - output is bounded by chunk and byte limits and reports truncation;
  - `kill`, Session close, and Runtime close terminate complete POSIX process
    groups with graceful-then-forced cleanup.
- Added a default direct-executable deny policy containing `rm`. A Host may
  replace the deny set through `AgentRuntime.open(denied_executables=...)`;
  Agent and Workspace state cannot mutate the Runtime-lifetime snapshot.
- Preserved the fixed model-visible `exec`, `output`, and `kill` surface and
  backend-neutral Execution Snapshot.

## Current state

- `cli-agent` supports one-shot and interactive multi-turn use through a real
  OpenAI-compatible Provider.
- A short command normally returns a terminal Snapshot. A command that outlives
  `wait_ms` returns a running Handle and continues for later `output` or
  `kill`.
- Direct, quoted, and absolute-path forms of a denied executable are rejected
  before Handle allocation or process creation. The default denies `rm`.
- The full suite has 95 passing tests. `uv sync --locked --check`, Ruff lint,
  and Ruff format checks pass.
- The parent architecture repository records the specification delta in commit
  `42e008e` (`docs(architecture): adopt unified execution control plane`).

## Known limits

- The first command inspector uses POSIX `shlex` and checks only the first
  token's path basename. It deliberately does not unwrap `env`, `command`,
  `sudo`, interpreters, generated scripts, or commands appearing only after a
  Shell operator.
- The deny policy is a command-admission guardrail, not deletion prevention,
  risk classification, human approval, or an operating-system sandbox.
- Milestone 04 is not implemented: concurrent `exec` calls in one Session do
  not yet enter a bounded at-most-one-running FIFO.
- Commands still inherit the complete cli-agent process environment, including
  direnv-loaded Provider credentials. Workspace Environment Request and Host
  Environment Grant filtering belong to milestone 05.
- Execution records are in memory only and are not restored after Runtime
  restart.

## Next

Implement milestone 04 around the admitted immutable Plan boundary:

1. add a configurable per-Session FIFO with default capacity 32;
2. run at most one Execution per Session while allowing cross-Session
   concurrency;
3. ensure policy denial consumes no queue capacity;
4. preserve Session-private Handle lookup and cleanup through the shared
   Execution Supervisor.
