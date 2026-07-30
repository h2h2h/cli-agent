# AEP-aligned Custom dispatch and ordered parallel scheduling

Status: implemented in the working tree on 2026-07-29.

This decision supersedes the lane-based scheduling model and refines the
earlier unified-dispatch design.

## Decision

cli-agent uses the AEP Session command model as its execution baseline and adds
only three architectural extensions:

1. integration behind the fixed `exec`, `output`, and `kill` Agent Runtime
   surface;
2. a Host-owned Policy gate before routing and admission;
3. trusted, bounded parallel scheduling for explicitly registered commands.

The execution path is:

```text
exec
  -> syntax-only Command Parser
  -> Policy
  -> Custom-first Command Router
  -> ordered Scheduler
  -> Kernel execution supervision
  -> Custom Driver or Shell Driver
```

## Custom-first dispatch

The Router looks up the first parsed command token in a Runtime-owned
`CustomCommandRegistry`. A match binds that exact handler into the immutable
Execution route. A miss falls back to the Shell Driver.

The default Custom registry contains `cd` and `export`. Future `tools` support
uses the same registry after the Capability View and Tool Environment exist.
Adding a Custom command does not add a model-visible schema.

This follows AEP's `CommandExecutor` registry model. Custom does not mean
`python -m`, and it does not imply a process:

- `cd` and `export` execute as Runtime handlers and mutate Session state;
- metadata reads such as future `tools list` may execute directly;
- future `tools run` may use the shared subprocess facility internally;
- ordinary Shell commands always use the Shell Driver's child process.

Consequently, process creation is a Driver or handler implementation detail,
not a routing axis.

## Unified Execution

Every allowed, admitted command has the same Execution State, output Cursor,
terminal states, cancellation API, and Session-private Handle. The Kernel
calls the Driver selected before admission and never branches on Driver kind.

Private coroutine and process adapters may differ in cancellation mechanics,
but they are not separate public Execution categories.

## Policy

Policy remains between syntax parsing and routing:

```text
CommandParseResult -> ExecutionDecision -> route
```

The current Policy may continue inspecting `executable_basename`. An allowed
Decision remains bound to the exact parse result it authorized. A denial
creates no Execution, Handle, queue entry, Driver resource, or side effect.

## Scheduling

Driver kind does not determine concurrency. Each resolved command instead has
one Runtime-trusted scheduling class:

- `SERIAL`, the default and the exact AEP-compatible behavior;
- `PARALLEL_SAFE`, granted only by Runtime configuration or a registered
  Custom command specification.

The Scheduler preserves submission order using barriers:

1. consecutive `PARALLEL_SAFE` commands at the queue head may run together up
   to the configured capacity;
2. a `SERIAL` command waits for all earlier running commands and then runs
   alone;
3. later parallel-safe commands cannot overtake an earlier serial command.

For example:

```text
parallel A, parallel B -> serial export -> parallel C, parallel D
```

executes as three ordered phases. Parallel Executions receive snapshots of the
Session cwd and custom environment. Session-mutating commands are serial.

Simple direct Shell invocations may be registered by executable basename.
Shell composition such as pipelines, redirection, command substitution, or
control operators always falls back to `SERIAL`. Agent-authored command text
cannot grant itself parallel authority.

## Ownership

- `EnvironmentKernel` owns the Parser, Policy, both stateless Driver services,
  the Custom registry, trusted scheduling configuration, and the owning
  Session's mutable execution state.
- Each command is routed independently; a Session is not bound to one Driver.
- Each Session owns one `EnvironmentKernel`; that Kernel owns cwd, custom
  environment, Scheduler, Execution States, and lifecycle cleanup.
- A resolved Custom route binds the selected command specification before
  admission, so later registry changes cannot rewrite admitted work.

## Current scope

The implementation installs AEP-style `cd` and `export`, Custom registration,
Shell fallback, and ordered parallel Shell configuration. `tools` remains a
future Custom handler because its Capability View and Workspace Tool
Environment do not yet exist; it must not be approximated by an unrelated
`python -m` wrapper.
