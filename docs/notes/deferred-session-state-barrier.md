# Deferred Session cwd/env barrier

The proposed per-Session Execution Scheduler initially distinguishes only two
execution lanes:

- Shell Driver Executions run serially, with at most one running per Session.
- Tool Driver Executions may run concurrently within a bounded, Host-configured
  budget.

This replaces a permanent global at-most-one-running rule with a deliberately
small first scheduling model. Milestone 04 remains Shell-only, so its immediate
observable behavior is still serial Shell execution. The distinction becomes
active when the Tool Driver is introduced.

The accepted scheduling context is documented in
[`../discussions/driver-aware-execution-scheduler.md`](../discussions/driver-aware-execution-scheduler.md).

A Session cwd/environment read-write barrier is deferred. The current Shell
Driver starts an independent child shell for each Execution from the
Runtime-selected Workspace cwd and process environment. A `cd` or `export`
inside that child affects only that child process and does not mutate persistent
Session state. Until persistent Session cwd or environment mutation exists,
adding generations, immutable state snapshots, reader/writer coordination, or
state barriers would protect state that the Runtime does not yet expose.

While this is deferred:

- the Runtime must not claim that a standalone `cd` or `export` persists across
  Executions;
- Shell Executions remain serial within one Session;
- Tool concurrency must not infer safety from Agent-authored Tool metadata;
- `output` and `kill` continue to address Session-private Execution records
  outside normal execution admission.

Revisit the barrier before adding any operation that persistently mutates
Session cwd or environment, or before concurrent Drivers consume mutable
Session state. That design must decide and test:

- when an Execution binds an immutable cwd/environment snapshot;
- whether state mutations wait for earlier Executions to finish or only for
  earlier state snapshots to be bound;
- how later Executions are prevented from overtaking a state mutation;
- how state generations interact with queued cancellation and Session close;
- how batch Tool Call dispatch preserves model-returned result order.
