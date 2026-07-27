# Unified Execution dispatch

## Question

`exec` is the single model-visible entry into the environment. The current
implementation sends every command to the host shell. Later milestones add
Kernel-owned command categories:

1. `tools list` / `tools info` / `tools run ...` route to the Capability View
   and Workspace Tool Environment.
2. Managed Workspace commands perform root-confined reads and optimistic
   mutations.
3. Everything else remains an ordinary shell command.

These operations have different semantics and need different Runtime state.
This document records how to preserve those distinctions while giving the
Agent one CLI-shaped environment and one execution lifecycle.

The companion
[Control plane and execution plane](./control-plane-and-execution-plane.md)
discussion places command inspection, immutable planning, and permission
enforcement in front of the execution lifecycle defined here.

## Design principles

### CLI-first

The Agent expresses environment operations as commands passed to `exec`.
Dynamic capabilities do not add model-visible schemas. The stable
`exec` / `output` / `kill` surface is the control plane for every command.

This is the important sense in which cli-agent is CLI-oriented. The Reference
CLI is only a thin host; the deeper invariant is that the Agent uses a
discoverable command environment rather than a changing function registry.

### Execution-unified

Commands may run differently, but every command accepted by `exec` becomes an
Execution with the same control contract:

- an opaque `exec_id`;
- a lifecycle state and terminal result;
- bounded, append-only stdout/stderr-style output;
- stable, non-destructive Cursor reads;
- wait, cancellation, and Session-release behavior;
- one backend-neutral Execution Snapshot.

`output` and `kill` operate only on this contract. They do not branch on the
kind of command that created the Execution.

### Driver-specific resources

Execution unifies lifecycle, not implementation mechanism. A private execution
driver owns the resources required to perform one category of command:

- a Shell driver owns a subprocess and its process group;
- a Tool driver owns the selected Tool invocation and its worker resources;
- a managed-command driver owns a Kernel operation such as an optimistic
  Workspace mutation.

The common Execution contract requires cancellable lifecycle cleanup, not an
operating-system process group from every driver. A synchronous managed command
may complete before cancellation can act; a Shell command requires graceful
and then forced process-group termination.

Drivers are an internal `EnvironmentKernel` seam, not a public plugin or
registration API. Adding a driver is an architectural change owned by the
Runtime, not a capability action available to the Agent.

## Options rejected

### A. Add `tools_call` as a fourth AEP Syscall

Lift Tool invocation out of `exec` and expose it as a sibling of `output` and
`kill`. This would provide structured arguments and results without parsing a
command string.

Rejected. `CONTEXT.md` fixes the model-visible syscall set at `exec`, `output`,
and `kill`, and the `Tool` definition explicitly avoids a model-visible Tool
schema. A fourth syscall whose shape varies with capabilities reintroduces the
dynamic function-registry design that AEP-native avoids.

### B. Make `shell` itself a Tool

Wrap every command as `tools run "shell.exec(...)"` so there is only one
implementation path.

Rejected. A Tool is a Python capability module under `tools/`, not an arbitrary
host command. Forcing Shell through Tool collapses the distinction between
user-authored capability files and ordinary Unix commands and makes the
environment less CLI-like.

### C. Resolve Kernel commands only through `PATH`

Ship a `tools` executable inside each Workspace Tool Environment and send every
command through the host shell.

Rejected as the default design. `tools list`, `tools info`, and `tools run`
need Kernel-owned state: the Capability View, trusted Capability Provenance,
validation results, and the Workspace Tool Environment. Moving that state
across a process boundary requires an IPC and authority design that is more
complex than routing a reserved Kernel command inside `EnvironmentKernel`.

This does not rule out a future shell proxy if true pipeline composition
justifies that complexity. It rules out treating `PATH` lookup as the core
Runtime boundary.

## Recommendation

Separate command routing from Execution supervision and concrete execution:

```text
                  exec("...")
                       |
                       v
              +----------------+
              | Command Router |
              +-------+--------+
                      |
          +-----------+------------+
          |                        |
          v                        v
   reserved Kernel command    ordinary command
          |                        |
          v                        v
   Tool / Managed driver      Shell driver
          |                        |
          +-----------+------------+
                      |
                      v
             +----------------------+
             | Execution Supervisor |
             | state / Cursor / log |
             | wait / cancel / close|
             +----------+-----------+
                        |
                        v
               output / kill
```

The Command Router determines which private driver understands a command. The
driver performs the work and emits normalized stdout/stderr-style output. The
Execution Supervisor owns the Session-visible record and all backend-neutral
lifecycle behavior.

The distinction matters:

- routing answers **what kind of command is this?**
- a driver answers **how is this command performed and cancelled?**
- Execution answers **how is accepted work observed and released?**

A table may be a convenient router implementation, but table-driven dispatch is
not itself an architectural requirement. The stable protocols and state
ownership are the seam.

## Result and failure representation

The Execution Snapshot is a control envelope, not a union of backend-specific
result objects. Adding a driver must not require `output` or `kill` to learn a
new result shape.

Drivers expose human-readable text or canonical JSON through the shared output
log and map completion to the common terminal states. For example, a managed
write can emit a canonical JSON diagnostic for `VERSION_CONFLICT`, and a Tool
can emit its structured success or failure result as canonical JSON. The
Snapshot continues to carry status, exit information, chunks, Cursor, and
truncation metadata.

If later evidence requires a machine-readable result field outside the output
log, it must be one shared field with backend-neutral semantics. A `kind`
discriminator plus backend-specific payloads is rejected because it leaks the
driver boundary back through the unified contract.

Failure normalization does not erase domain diagnostics. A process crash, Tool
exception, and optimistic-write conflict may all produce a failed terminal
Execution, while their canonical output retains the information needed to
understand and recover from that failure.

## Elegance tests

The design is unified only when all of the following remain true:

1. Adding an internal driver does not add a model-visible syscall.
2. `output`, `kill`, and Session cleanup do not branch on driver type.
3. The Execution Snapshot does not gain driver-specific fields.
4. Shell, Tool, and managed commands retain their distinct validation,
   authority, execution, and cancellation semantics.
5. Capability state and trusted provenance remain inside the Kernel.

`CONTEXT.md` already defines Execution independently from an Agent turn or
shell process. This design makes that lifecycle boundary explicit without
claiming that every accepted command is implemented by the same mechanism.

## Mapping to milestones

### Milestone 03: long-running Executions

Build the backend-neutral Execution state, output buffer, Cursor, Snapshot,
wait, cancellation, and release lifecycle. Implement only the Shell driver.
Keep subprocess and process-group details behind that driver rather than making
them the public shape of Execution.

Milestone 03 does not need a general Command Router, Tool driver, managed
driver, public driver protocol, or new Snapshot payload.

### Milestone 04: isolated concurrent Sessions

Place the FIFO queue and at-most-one-running rule around the shared Execution
lifecycle. Session close cancels queued Executions and delegates cleanup of the
running Execution through its driver without exposing driver type.

### Milestone 06: conflict-safe Workspace mutations

Define the reserved managed-command grammar and add its internal driver when
the command contract is known. Managed operations use the same Execution
control envelope, but their optimistic version checks and atomic mutations
remain Kernel-owned semantics.

### Milestone 10: isolated Tool Environment

Define the reserved `tools` command grammar and add the Tool driver. Capability
View access, validation, provenance, dependency environment selection, and Tool
result encoding remain inside that driver and the Kernel.

MCP Tools continue through the Tool driver after Repertoire Reconciliation.
They do not introduce an MCP-specific execution driver or model-visible path.

## Open questions for the implementing milestones

- **Reserved command namespace.** Should Kernel commands retain the documented
  `tools ...` and Workspace command heads, or use one explicit namespace to
  avoid collisions with host executables?
- **Command grammar.** Which top-level quoting and argument forms are accepted?
  Are pipelines, redirections, `env` prefixes, and shell control operators
  intentionally unsupported for Kernel commands, or will a later proxy provide
  composition?
- **Tool process model.** Does each `tools run` use a fresh worker, or does one
  persistent Workspace process serve multiple invocations? The choice must
  preserve cancellation, module-state isolation, and Workspace dependency
  isolation.
- **Canonical result encoding.** What JSON envelope and exit semantics represent
  Tool success, Tool failure, managed-command success, and recoverable domain
  conflicts in the shared output log?
- **Cancellation boundary.** Which synchronous Kernel operations can be
  cancelled before commit, and which become terminal once an atomic mutation
  begins?
