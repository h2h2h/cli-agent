# Session-scoped Environment Kernel

Status: accepted
Accepted: 2026-07-30

The historical Driver terminology below refers to the current Command Handler
contract defined by
[RFC-0007](../rfcs/proposed/RFC-0007-unified-command-routing-and-execution-refactor.md).
There is no public Driver API or Tool-specific scheduling lane.

## Decision

Every active Agent Session owns exactly one `AgentLoop` and one
`EnvironmentKernel`. Their lifetimes are identical.

`EnvironmentBinding`, hidden Kernel Session IDs, the Kernel Session registry,
and the separate `EnvironmentSession` object are removed. `AgentLoop` dispatches
the fixed `exec`, `output`, and `kill` calls directly to its Session's Kernel.

```text
AgentRuntime
└── sessions[host_session_id]
    ├── AgentLoop
    └── EnvironmentKernel
        ├── cwd and custom environment
        ├── Execution Scheduler
        ├── Execution States
        └── Parser, Policy, Router, and Drivers
```

`AgentRuntime` remains the only Host-facing lifecycle owner. It prepares the
Workspace and immutable environment snapshot once, creates a Kernel when a
Session is first used, and closes that Kernel when the Session is removed.
Reusing a closed Host Session ID creates a fresh Loop and Kernel.

## Rationale

The earlier shape copied AEP's independently addressed
`open_session(session_id)` service boundary:

```text
Runtime Session -> EnvironmentBinding -> hidden id -> Kernel registry
                -> EnvironmentSession
```

In cli-agent all of those objects lived in one process and the Runtime Session
and Kernel Session always had a one-to-one lifetime. The hidden ID and second
registry expressed no independent identity, ownership, transport, or
authorization boundary.

Per-Session state remains necessary, but it is now state owned directly by the
Session's Kernel. Cross-Session isolation is structural: different Sessions
have different Kernels and therefore different cwd, environment, Scheduler,
Handle namespace, output, and cancellation state.

## Execution implementation

There is no separate Execution Supervisor object. `EnvironmentKernel`
coordinates Driver preparation, execution, output waiting, cancellation,
promotion, and close cleanup.

Each admitted Execution is still represented by one private
`_ExecutionState`. This is a real one-to-many relationship: one Kernel owns
many live Execution States. The type remains in `execution.py` so Scheduler and
Kernel can share it without a circular dependency.

## Workspace-scoped resources

A Session-scoped Kernel must not duplicate live Workspace resources. Workspace
bootstrap, the immutable `.workspace/env` snapshot, future Capability View
mount lifecycle, and shared MCP clients belong to `AgentRuntime`. A Kernel
receives the Workspace references and shared dependencies that it needs.

If a later feature requires Runtime-wide admission or connection limits, the
Runtime may inject a shared limiter into its Session Kernels. That does not
reintroduce Kernel Session IDs or a Kernel-owned Session registry.

## Preserved invariants

- The model-visible syscall set remains `exec`, `output`, and `kill`.
- Parser → Policy → Router → Scheduler → Driver remains the execution path.
- A denial creates no Handle, queue entry, Execution State, or Driver resource.
- Handles are private to the Kernel that created them.
- Different Session Kernels share ordinary Workspace filesystem visibility.
- Session close cancels queued and running work and clears transient state.
- Runtime close closes every active Session Kernel.
