# Control plane and execution plane

Status: accepted for specification
Accepted: 2026-07-28
Normative follow-up: architecture decision 16

This discussion records the rationale that was accepted for incorporation into
the architecture specification. Where it differs from the normative
architecture decision or RFC, those later records govern.

## Question

cli-agent is converging on one CLI-shaped environment surface:

- the model submits commands through `exec`;
- `output` and `kill` address accepted work through an Execution Handle;
- Shell commands, built-in Tool commands, and managed Workspace commands use
  different internal mechanisms while sharing one Execution lifecycle.

That common entry point is also the natural place to decide whether work may
start. How should command routing, permission policy, immutable planning, and
execution be separated without changing the fixed model-visible syscall set or
claiming an operating-system sandbox?

This discussion adapts the control-plane/execution-plane method from
[Agent Bash 工具顶层设计方法论](<../references/Agent Bash 工具顶层设计方法论.pdf>)
to cli-agent's AEP-native architecture.

## Decision summary

Introduce two logical planes inside `EnvironmentKernel`:

- the **control plane** validates, routes, inspects, authorizes, plans, and
  admits requested work without executing it;
- the **execution plane** consumes an immutable `ExecutionPlan`, performs the
  work through a private driver, and owns the observable Execution lifecycle.

The boundary between the planes is an immutable `ExecutionPlan`.

The first implementation has only a Shell driver and a small host-owned
`ExecutionPolicy`. That policy supports immediate `ALLOW` or `DENY` decisions
and denies recognized direct invocations of configured executable names,
initially including `rm`.

This first policy is a command-admission guardrail. It is not a claim that the
Runtime can detect or prevent every operation that deletes files, and it does
not change the existing rule that the Runtime is not an OS sandbox.

## Architectural constraints

The proposal preserves these existing constraints:

1. `AgentRuntime`, `AgentLoop`, and `EnvironmentKernel` remain the three deep
   modules. The two planes are internal Kernel structure, not new services.
2. The model-visible syscall set remains exactly `exec`, `output`, and `kill`.
3. Dynamic Skills, Tools, Library content, and MCP projections do not create
   model-visible schemas.
4. Session-private Execution Handles remain the authority for `output` and
   `kill`.
5. Execution state remains in memory and is not restored after Runtime restart.
6. The Runtime enforces logical authorization and ownership but does not claim
   filesystem, network, process, or resource containment without an external
   sandbox.

Adopting a built-in enforcement point does revise the current MVP decision that
commands execute without allow/deny policy. If this proposal is accepted,
`CONTEXT.md`, the architecture RFC, and the relevant implementation ticket must
be updated explicitly rather than treating the change as an implementation
detail.

## Architecture

```text
AgentLoop
    |
    | exec(command)
    v
EnvironmentBinding
    |
    v
+------------------------------------------------------+
| EnvironmentKernel                                    |
|                                                      |
|  Control plane                                       |
|                                                      |
|  validate request                                    |
|      -> route and inspect                            |
|      -> build ExecutionPlanCandidate                 |
|      -> ExecutionPolicy                              |
|           |                                          |
|           +-- DENY -> policy_denied                  |
|           |            no exec_id, no queue entry    |
|           |                                          |
|           +-- ALLOW                                  |
|                 -> freeze ExecutionPlan              |
|                 -> Session admission                 |
|                              |                       |
|                    immutable ExecutionPlan           |
|                              v                       |
|  Execution plane                                     |
|                                                      |
|  Session queue                                       |
|      -> Execution Supervisor                         |
|      -> Shell / Tool / Managed driver                |
|      -> output buffer / Cursor / state               |
|      -> cancellation / cleanup                       |
|                                                      |
+------------------------------------------------------+
              |
              v
       Execution Snapshot
       output / kill
```

The planes answer different questions:

- the control plane answers **what operation is requested, and may it start?**
- the execution plane answers **how is the approved operation run, observed,
  cancelled, and released?**

Command routing belongs to the control plane. Driver mechanics belong to the
execution plane. Execution is the shared lifecycle across both.

## Domain objects

### ExecutionRequest

`ExecutionRequest` is the validated, Session-scoped request derived from one
model `exec` call. It contains the original requested values:

- raw command;
- requested wait duration;
- requested output limit;
- bound Session and Workspace context.

It is not yet accepted work and has no `exec_id`.

### CommandAnalysis

`CommandAnalysis` records the facts the control plane can establish without
performing the operation:

- selected route or candidate driver;
- recognized executable names or Kernel operation;
- normalized arguments available at that layer;
- Shell constructs or wrappers the inspector recognizes;
- managed paths, Tool name, and Capability Provenance when applicable;
- uncertainty or unsupported syntax relevant to policy.

Analysis is not proof of all eventual side effects. In particular, arbitrary
Shell commands and executable Tools can compute behavior dynamically.

### ExecutionPlanCandidate

The candidate combines the requested operation with the proposed execution
configuration before authorization:

- exact command or structured Kernel operation;
- selected private driver;
- effective working directory;
- effective filtered environment reference;
- wait and output policies;
- facts available for policy evaluation;
- future optional sandbox and resource profiles.

Building a candidate must not start a process, mutate the Workspace, import and
execute a Tool, or allocate an Execution Handle.

### PolicyDecision

The first version has two decisions:

```text
ALLOW
DENY
```

A denial contains a stable rule identifier and a safe reason suitable for the
model-visible `policy_denied` error. Detailed Host diagnostics may contain more
context but must not expose secrets.

`ASK` is deferred until the Host callback, cancellation, timeout, and approval
record semantics are designed. Adding `ASK` later must not add another
model-visible syscall.

### ExecutionPlan

An allowed candidate is frozen as an immutable `ExecutionPlan`. The execution
plane must execute that exact plan; it must not regenerate or rewrite the
command after authorization. The selected driver may interpret the exact
payload only according to the execution mechanism recorded in the Plan.

The Plan records at least:

- the exact operation to execute;
- the selected driver;
- cwd and filtered environment reference;
- output and wait configuration;
- the policy decision or decision identifier;
- any execution constraint selected by the control plane.

Immutability prevents a future approval or authorization decision for command
A from being reused to execute command B. It does not make dynamic Shell
expansion statically knowable.

### Execution

Reuse the existing `Execution` domain object rather than introducing a
duplicate `Task` abstraction. An Execution begins only after an allowed Plan is
admitted and assigned an `exec_id`.

Execution continues to own the backend-neutral lifecycle:

- queued, running, and terminal state;
- bounded append-only output;
- stable Cursor reads;
- terminal exit information;
- cancellation and Session-release behavior.

Driver-specific resources remain private. A Shell driver owns a process group;
a Tool driver may own a worker; an atomic managed command may have no
long-lived process to terminate.

## Control plane

The control plane is read-only with respect to the requested operation until
authorization succeeds. Its initial pipeline is:

```text
validate
    -> route
    -> inspect
    -> build candidate
    -> authorize
    -> freeze plan
    -> admit
```

### Command routing

Routing selects an internal command category:

- ordinary commands use the Shell driver;
- reserved managed Workspace commands later use a managed-command driver;
- reserved `tools` commands later use a Tool driver.

Milestone 03 does not require a general routing registry. With only the Shell
driver present, routing may be trivial while retaining the Plan boundary.

Drivers are a closed, private Kernel seam. Workspaces and capabilities cannot
register new drivers or bypass policy.

### Execution Policy

Policy is supplied or configured by the Host and enforced at the Kernel
admission boundary. The Agent and Workspace may observe a safe denial reason
but cannot modify or relax the effective policy.

The effective policy is selected when the Runtime opens and remains fixed for
that Runtime lifetime. The Runtime default is the direct dangerous-command
guard below with `rm` in its deny set. An embedding Host may deliberately
replace that default policy at open time; neither an Agent request nor a
Workspace mutation can do so.

Conceptually:

```python
class ExecutionPolicy(Protocol):
    async def authorize(
        self,
        candidate: ExecutionPlanCandidate,
        context: AuthorizationContext,
    ) -> PolicyDecision: ...
```

The interface may be asynchronous from the beginning so a later Host approval
adapter does not require moving the enforcement seam. The first implementation
returns immediately.

Policy rules should target stable operation facts where available:

```text
shell.execute
tool.list
tool.inspect
tool.run
workspace.read
workspace.write
workspace.remove
```

Rules may later constrain executable name, Tool name, Capability Provenance,
Managed Path, network profile, or other planned capabilities.

### Admission invariants

1. No requested side effect occurs before `ALLOW`.
2. `DENY` returns `policy_denied` without an `exec_id` or queue entry.
3. A policy error fails closed and emits a safe internal diagnostic.
4. Policy allows or denies; it never silently rewrites the requested command.
5. The admitted Plan is the same Plan passed to the execution plane.
6. Every driver is reachable only after the common policy gate.
7. Session close, Runtime close, cancellation, and forced cleanup never require
   policy approval.
8. Effective policy is Host-owned and snapshotted for a defined lifecycle; a
   Workspace write cannot expand current authority.

## Execution plane

The execution plane accepts only admitted immutable Plans.

### Execution Supervisor

The Supervisor owns:

- Execution creation and state transitions;
- Session FIFO integration;
- wait behavior;
- output Buffer and Cursor coordination;
- driver start and cancellation;
- terminal snapshots;
- Session and Runtime cleanup.

`output` and `kill` address this state through a Session-private Handle. They do
not rerun command policy:

- `output` is authorized by possession of a Handle in the bound Session;
- `kill` may only reduce activity and must remain available for cleanup;
- a foreign or nonexistent Handle retains the same not-found behavior.

### Drivers

Drivers execute Plans but do not decide whether Plans are allowed:

| Driver | Mechanism | Owned execution resource |
|---|---|---|
| Shell | host subprocess | process group |
| Tool | Workspace Tool Environment | worker or Tool invocation |
| Managed command | Kernel operation | optimistic operation state |

All drivers emit normalized stdout/stderr-style output and completion into the
shared Execution contract. Driver-specific fields do not leak into
`ExecutionSnapshot`.

MCP Tools continue through the Tool driver. They do not introduce an
MCP-specific execution or permission path.

## First policy: direct dangerous-command guard

The first policy is deliberately small. It denies positively recognized direct
invocations whose executable basename is in a Host-configured deny set. The
initial set includes:

```text
rm
```

Additional names such as `rmdir` or `unlink` may be configured after their
desired compatibility impact is decided.

Minimum examples:

```text
rm file                  -> DENY
rm -rf build             -> DENY
/bin/rm file             -> DENY
pytest -q                -> ALLOW
```

The milestone 03 inspector uses POSIX `shlex` tokenization and examines only
the first token of the submitted command. If its path basename is in the deny
set, the command is denied. Leading whitespace, quoting of the executable name,
and absolute executable paths are therefore covered. A tokenization failure or
any command whose first token is not denied is not a policy failure: this
specific positive-match rule does not match it.

Consequently, `rm file`, `"rm" file`, and `/bin/rm file` are recognized, while
`env rm file`, `command rm file`, `sh -c 'rm file'`, and an `rm` appearing only
after a Shell operator are outside the first rule's coverage. The complete raw
string remains the exact Shell-driver payload; inspection never rewrites it.

Wrapper commands, pipelines, conditionals, substitutions, generated scripts,
and nested interpreters require structured Shell analysis to cover reliably.

The initial rule does not claim to prevent deletion effects such as:

```text
python -c "import os; os.remove('file')"
find . -delete
tools run cleanup
bash generated-script.sh
```

An unrecognized command being permitted means only that the first policy did
not match it. It is not a safety classification.

This is why the feature is described as a command-admission guardrail rather
than filesystem permission enforcement.

## Authorization and sandboxing

Authorization and sandboxing are separate:

```text
Execution Policy
    -> decides whether a Plan may start

Sandbox
    -> limits what an allowed Plan can affect at runtime
```

The first version adds only authorization. Shell commands and executable Tools
retain the filesystem, network, process, and system authority of the embedding
Host process.

A future sandbox belongs in the execution plane:

```text
ExecutionPlan.sandbox_profile
    -> driver
    -> sandbox adapter
    -> process or worker
```

The control plane may select or request a profile. The execution plane must
enforce it. A sandbox failure must never cause an automatic fallback to less
restricted Host execution.

## State and result semantics

Do not copy a large control-plane state machine into model-visible Execution
status.

- Validation or policy denial occurs before Execution acceptance and returns a
  structured Tool error.
- Waiting for future Host approval is control-plane state, not a running
  Execution.
- `exec` wait timeout means that an Execution continues running; it is not a
  terminal timeout.
- Future sandbox, resource, and command failures may use a shared failure
  category or canonical output without adding driver-specific statuses.
- Existing `queued`, `running`, `exited`, `failed`, and `killed` states remain
  the compact Execution lifecycle unless separate evidence requires a schema
  change.

## Implementation plan

### Milestone 03: establish the plane boundary

Control plane:

- derive `ExecutionRequest` from validated `exec` arguments;
- build a Shell-only `ExecutionPlanCandidate`;
- call the immediate `ExecutionPolicy`;
- return `policy_denied` for recognized denied commands;
- freeze an allowed immutable `ExecutionPlan`;
- pass only that Plan to the execution plane.

Execution plane:

- implement the Shell driver;
- implement long-running Execution state;
- capture bounded incremental output;
- preserve stable Cursor reads;
- terminate the owned process group through `kill`, Session close, and Runtime
  close.

Do not add a general Router, Tool driver, managed driver, approval manager,
sandbox, persistent audit store, or public driver protocol in milestone 03.

### Milestone 04: queue and Session isolation

- queue admitted Plans in the bound Session;
- preserve at-most-one-running Execution per Session;
- let different Sessions execute concurrently;
- keep Handle lookup and cleanup Session-private;
- ensure policy denial consumes no queue capacity.

### Milestone 05: filtered environment

- construct Plans only from the effective filtered Session environment;
- keep Provider credentials outside Agent execution unless explicitly granted;
- prevent policy diagnostics and audit facts from exposing environment values.

### Milestone 06: managed Workspace commands

- define structured managed-command routes and Plan facts;
- authorize `workspace.read`, `workspace.write`, and `workspace.remove`
  independently;
- evaluate Managed Paths before admission;
- keep optimistic version comparison and mutation atomic inside the driver;
- retain the distinction between managed guarantees and arbitrary Shell writes.

### Milestone 10: Tool commands

- define structured `tools list`, `tools info`, and `tools run` routes;
- expose Tool name, operation, validation state, and trusted provenance to
  policy;
- keep Tool dependency and process mechanics in the Tool driver;
- preserve one common Execution and permission path for generated MCP Tools.

### Later work

Only after the immediate allow/deny seam is proven:

- structured Shell AST and wrapper analysis;
- risk categories;
- `ASK` with Host approval callbacks;
- immutable approval records;
- sandbox profiles and resource limits;
- semantic failure categories;
- Host-visible policy and execution diagnostics;
- persisted output Artifacts and retention;
- event-driven completion notifications.

## Acceptance criteria for the first version

1. Every `exec` request passes the common policy gate before process creation.
2. A recognized direct `rm` invocation returns `policy_denied`.
3. A denied request creates no process, Handle, Execution record, or queue item.
4. An allowed request is executed from the same immutable Plan that policy
   evaluated.
5. Policy cannot be relaxed by a Workspace mutation or model command.
6. Policy failure does not fail open.
7. `output`, `kill`, Session close, and Runtime close preserve their existing
   authority and cleanup behavior.
8. Shell, later managed commands, and later Tool commands cannot bypass the
   shared admission seam.
9. Tests document the exact Shell forms the first deny rule covers.
10. Documentation states that indirect deletion and other effects remain
    possible without an OS sandbox.

## Non-goals for the first version

- proving the semantic safety of arbitrary Shell;
- preventing every way to delete or overwrite a file;
- malware or prompt-injection classification;
- OS-level filesystem, network, process, or resource containment;
- user approval UI;
- automatic privilege escalation;
- dynamic policy registration by the Agent;
- a public Execution driver plugin system;
- durable Execution recovery after Runtime restart.

## Open questions

1. Is the first effective policy a secure default supplied by Runtime, a
   required Host argument, or a Host override of Runtime defaults?
2. Which direct Shell forms must the first inspector recognize beyond an
   executable basename and absolute executable path?
3. What is the explicit behavior for unsupported or ambiguous Shell syntax?
4. Should policy be snapshotted per Runtime or per Session?
5. Which Host-facing diagnostic surface records policy decisions without
   persisting secrets?
6. What reserved CLI namespace will distinguish Kernel commands from host
   executables?
7. Which failure facts belong in canonical output, and which require a future
   backend-neutral result field?

## Relationship to unified execution dispatch

The companion
[Unified Execution dispatch](./unified-execution-dispatch.md)
discussion defines the CLI-first command surface, backend-neutral Execution,
and private execution drivers.

This proposal adds the missing admission boundary:

```text
CLI command
    -> Router: what operation is requested?
    -> Policy: may the operation start?
    -> ExecutionPlan: freeze the authorized operation
    -> Execution: how is it observed and managed?
    -> Driver: how is it performed?
```

Together, the two discussions establish one path for Shell, Tool, and managed
Workspace commands without confusing command authorization with OS isolation.
