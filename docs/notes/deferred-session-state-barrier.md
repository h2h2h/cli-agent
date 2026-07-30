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

A general Session cwd/environment read-write barrier remains deferred. The
current Shell Driver starts an independent child shell for each Execution from
the Runtime-selected Workspace cwd and process environment.

Milestone 05 deliberately adds one narrower exception modeled on AEP: a direct
top-level `export KEY=VALUE ...` command mutates the current Environment
Session's in-memory custom environment in the serial Shell lane. A standalone
`cd` and an `export` nested inside another shell expression still affect only
that child process.

While the general barrier is deferred:

- the Runtime must not claim that a standalone `cd` or a nested Shell `export`
  persists across Executions;
- only the reserved top-level export grammar mutates Session state;
- Shell-lane FIFO guarantees that a later Shell Execution sees a completed
  earlier export;
- each Shell Execution binds `dict(os.environ) | session.env` when it starts;
- Shell Executions remain serial within one Session;
- future Drivers in other lanes receive no global ordering guarantee relative
  to a Shell-lane export unless a later milestone introduces one;
- Tool concurrency must not infer safety from Agent-authored Tool metadata;
- `output` and `kill` continue to address Session-private Execution records
  outside normal execution admission.

Revisit the barrier before adding persistent Session cwd mutation, broader
Shell-state emulation, or a cross-lane ordering guarantee for mutable Session
state. That design must decide and test:

- when a non-Shell Execution binds current cwd and Session environment state;
- whether state mutations wait for earlier Executions to finish or only for
  earlier state snapshots to be bound;
- how later Executions are prevented from overtaking a state mutation;
- how state generations interact with queued cancellation and Session close;
- how batch Tool Call dispatch preserves model-returned result order.
