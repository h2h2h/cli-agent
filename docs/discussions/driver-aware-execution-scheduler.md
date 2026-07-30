# Driver-aware per-Session Execution Scheduler

Status: superseded on 2026-07-29 by
[AEP-aligned Custom dispatch and ordered parallel scheduling](./aep-aligned-custom-dispatch-and-parallel-scheduling.md).

## Question

Should a Session serialize every Execution, or should it retain deterministic
Shell ordering while allowing bounded parallel Tool execution returned by
modern Model Providers?

## Context

Milestone 03 established long-running, backend-neutral Executions with stable
Handles, incremental output, cancellation, and a Shell Driver. The original
Session design placed every admitted Decision in one FIFO and allowed at most
one running Execution per Session.

That rule gives one simple lifecycle but also serializes operations that a
Model Provider may return as independent parallel Tool Calls. In particular, a
long-running Shell Execution would prevent later read or search Tools from
starting even when they do not require the Shell resource.

The current Runtime does not expose persistent Session cwd or environment
mutation. Every Shell Execution starts an independent child shell from the
Runtime-selected cwd and process environment; `cd` and `export` inside that
child do not affect a later Execution.

## Goals

- Preserve at-most-one-running Shell Execution per Session.
- Allow future Tool Driver Executions to run concurrently within an explicit,
  bounded Host-configured budget.
- Avoid head-of-line blocking between independent Driver lanes.
- Preserve model-returned Tool Call and Tool Result order in Conversation
  History.
- Keep policy denial outside Execution admission and queue capacity.
- Preserve the fixed model-visible `exec`, `output`, and `kill` surface.
- Keep Handle lookup, cancellation, and cleanup Session-private and
  backend-neutral.

## Non-goals

- Persistent Session `cd` or `export` semantics.
- A cwd/environment generation or read-write barrier.
- Inferring concurrency safety from Agent-authored Tool metadata.
- Classifying arbitrary Shell commands as read-only.
- Defining Managed Driver concurrency before its command contract exists.
- Adding a public Scheduler or Driver extension protocol.

## Options

### One global FIFO and one running Execution

Every allowed Decision enters one Session FIFO and only its head may run.

Advantages:

- Smallest state machine.
- Submission and start order are identical.
- Driver type does not affect scheduling.

Disadvantages:

- A long-running Shell process blocks all later Tool work.
- Parallel Tool Calls from a Model Provider are reduced to serial execution.
- Adding a bounded Tool worker model provides no same-Session throughput gain.

### Driver-aware bounded Scheduler

Every allowed Decision enters one bounded per-Session Scheduler. The Scheduler
assigns work to Runtime-trusted Driver lanes:

- the Shell lane has capacity one;
- the Tool lane has a bounded, Host-configured capacity greater than one when
  Tool concurrency is enabled.

Admission remains ordered. Work in one lane remains FIFO, while a runnable item
in another lane may bypass an item waiting for its own lane. Milestone 04 is
Shell-only, so the first implementation still behaves as a serial Shell FIFO.

Advantages:

- Preserves deterministic Shell ordering.
- Allows bounded parallel Tool work without adding model-visible schemas.
- Prevents a busy Shell lane from causing Tool-lane head-of-line blocking.
- Keeps future concurrency policy inside the Environment Kernel.

Disadvantages:

- Running Executions are no longer globally ordered.
- Queue selection, cancellation, and close require lane-aware tests.
- Tool Call batching must preserve Conversation History order independently
  from completion order.

### Run every admitted Decision concurrently

Start every admitted Decision subject only to one total task limit.

Advantages:

- Highest immediate concurrency.
- Small admission scheduler.

Disadvantages:

- Multiple arbitrary Shell commands may race in one Session.
- It provides no stable place for Driver-specific resource budgets.
- Completion timing rather than Runtime rules determines observable ordering.

## Decision

Use the driver-aware bounded Scheduler.

```text
Model Tool Calls in returned order
                |
                v
CommandParseResult -> ExecutionDecision
                |
                | DENY: no Handle and no capacity
                v
Per-Session Execution Scheduler
        |                       |
        v                       v
Shell lane                 Tool lane
capacity = 1               bounded Host budget
FIFO within lane           FIFO admission, parallel running
        |                       |
        v                       v
Shell Driver               Tool Driver
```

The scheduling class is derived from the Runtime-trusted route selected in the
`CommandParseResult`; an Agent-authored Tool cannot grant itself concurrent
authority through metadata. An `ExecutionDecision` may further restrict
execution but cannot rewrite the selected operation.

The pending queue defaults to capacity 32. Running Executions are governed by
their lane budgets rather than counted as pending queue entries. When the
pending queue is full, admission fails immediately with `queue_full`. A denied
Decision never becomes an Execution and consumes neither pending nor running
capacity.

## Ordering

Submission order, start order, completion order, and history order are distinct:

- submission sequence is assigned in model-returned Tool Call order;
- items in the same lane start in FIFO order;
- a runnable Tool item may bypass a Shell item waiting for the Shell lane;
- Executions in different lanes may complete in any order;
- AgentLoop appends Tool Results in the original Tool Call order.

For example:

```text
running: Shell A
pending: Shell B, Tool C, Tool D

Shell A  =========================
Tool C      =======
Tool D       =========
Shell B                            ==============
```

This is an ordered, work-conserving Scheduler rather than a queue-head-only
global FIFO.

## AgentLoop batch dispatch

AgentLoop may submit complete Tool Calls from one Assistant Message as an
ordered batch. Environment admission assigns their submission sequence in that
order, while eligible Driver work may proceed concurrently. AgentLoop waits for
the initial results and constructs one `ToolResultMessage` in original call
order, regardless of completion order.

`output` and `kill` do not enter normal execution admission. They address
Session-private Execution States directly through the Session Kernel.

## Lifecycle

- Killing a pending Execution removes it and marks it killed.
- Killing a running Execution delegates cancellation to its Driver and releases
  its lane capacity after cleanup.
- Session close cancels every pending Execution and asks its Kernel to clean up
  all running Driver resources.
- Runtime close performs the same operation for every Session.
- Foreign and nonexistent Handles return the same not-found result.

No cleanup path repeats Execution Policy or branches in the host-facing
Runtime on Driver type.

## Deferred cwd/environment barrier

Persistent Session cwd/environment mutation and its required snapshot,
generation, and ordering semantics are deferred. The constraints and revisit
conditions are recorded in
[`../notes/deferred-session-state-barrier.md`](../notes/deferred-session-state-barrier.md).

## Milestone mapping

### Milestone 04

- Add the bounded per-Session Scheduler and Shell lane with capacity one.
- Preserve cross-Session Shell concurrency.
- Prove queue capacity, Session-private Handles, cancellation, and close.
- Establish lane-aware scheduling without introducing a Tool Driver.

### Milestone 06

- Select the Managed Driver scheduling behavior when its closed command grammar
  and optimistic mutation semantics are implemented.

### Milestone 10

- Add the bounded Tool lane and choose its default concurrency budget.
- Prove same-Session Tool concurrency and Shell/Tool head-of-line avoidance.
- Preserve model-returned Tool Result order under out-of-order completion.

## Open questions

- What default per-Session Tool concurrency budget should Milestone 10 use?
- Which Managed operations, if any, may use a concurrent lane?
- Should Runtime-provided Workspace and Library read/search operations use the
  Tool lane or receive a separately specified Managed scheduling behavior?
- Should AgentLoop batch dispatch land with the Shell-only Scheduler or with the
  first concurrent Tool Driver?
