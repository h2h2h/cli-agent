# Control plane and execution plane

Status: accepted for specification
Accepted: 2026-07-28
Normative follow-up: architecture decision 16

The lane-based scheduling references are superseded by
[AEP-aligned Custom dispatch and ordered parallel scheduling](./aep-aligned-custom-dispatch-and-parallel-scheduling.md).
The remaining Driver and Tool-lane terminology in this historical discussion
is superseded by
[RFC-0007](../rfcs/proposed/RFC-0007-unified-command-routing-and-execution-refactor.md),
which defines Command handlers, Tool Catalog scheduling facts, and one global
Scheduler barrier model.
The lifecycle shape is amended by
[Session-scoped Environment Kernel](./session-scoped-environment-kernel.md):
each Agent Session owns one Kernel and there is no Environment Binding or
Kernel-owned Session registry.

This discussion records the rationale that was accepted for incorporation into
the architecture specification. Where it differs from the normative
architecture decision or RFC, those later records govern.

[RFC-0001: Host-mediated execution approval](../rfcs/approved/RFC-0001-host-mediated-execution-approval.md)
supersedes this discussion's immediate-only `ALLOW` / `DENY` Policy and its
planned Agent-visible managed Workspace command grammar. Policy evaluation now
supports `ALLOW`, `DENY`, and `ASK`; only a final allow-only
`ExecutionDecision` may cross into routing and admission. Ordinary Workspace
file mutations remain CLI operations, so optimistic conflict detection is not
promised for arbitrary Shell writes.

[RFC-0008](../rfcs/proposed/RFC-0008-shell-ast-pluggable-policy-and-guided-exploration.md)
further supersedes the `ExecutionDecision` and dedicated Approver boundary
described below. The Router now resolves `ShellParseResult` directly, Policy
is an optional Host plugin that runs after routing, and `ASK` is answered
through the always-present Host-owned `UserInteraction` channel instead of a
dedicated approval gate with capacity and timeout.

## Question

cli-agent is converging on one CLI-shaped environment surface:

- the model submits commands through `exec`;
- `output` and `kill` address accepted work through an Execution Handle;
- Shell commands and built-in Tool commands use different internal mechanisms
  while sharing one Execution lifecycle.

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
- the **execution plane** consumes an allowed immutable `ExecutionDecision`,
  performs the work through a private driver, and owns the observable Execution
  lifecycle.

The boundary between the planes is an allowed immutable `ExecutionDecision`.

The current implementation has a Shell driver and a small Host-owned
`ExecutionPolicy`. Policy evaluates `ALLOW`, `DENY`, or `ASK`; `ASK` must be
resolved by the Host before a final allow-only Decision exists. The default
asks for a narrow set of recognized direct filesystem mutators and otherwise
allows.

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
+------------------------------------------------------+
| Session-scoped EnvironmentKernel                     |
|                                                      |
|  Control plane                                       |
|                                                      |
|  parse command                                       |
|      -> CommandParseResult                           |
|      -> ExecutionPolicy.decide                       |
|           |                                          |
|           +-- DENY -> policy_denied                  |
|           |            no exec_id, no queue entry    |
|           |                                          |
|           +-- ALLOW                                  |
|                 -> Session admission                 |
|                              |                       |
|                  immutable ExecutionDecision        |
|                              v                       |
|  Execution plane                                     |
|                                                      |
|  Session Execution Scheduler                         |
|      -> Kernel execution supervision                 |
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

### CommandParseResult

`CommandParseResult` is the immutable result of syntax inspection for one
Session-scoped model `exec` call. It records the exact raw command, tokenization
result, recognized direct executable basename, and generic Shell composition
facts without performing or classifying the command.

It is not yet accepted work and has no `exec_id`. Parsing is not proof of all
eventual side effects: arbitrary Shell commands and executable Tools can
compute behavior dynamically. Parsing must not start a process, mutate the
Workspace, import and execute a Tool, or allocate an Execution Handle.

The first Policy intentionally used only the executable basename. RFC-0002
later added syntax-only recognition for explicit file output redirection and
in-place `sed` while retaining the same immutable parse and authorization
boundary. After a final allowed Decision, the Router selects a Runtime-trusted
scheduling class and Driver. A future Policy that independently authorizes
structured Tool operations must move their trusted semantic recognition before
that Policy without changing the execution-plane contracts below.

### PolicyEvaluation and ExecutionDecision

Policy has three read-only evaluations:

```text
ALLOW
DENY
ASK
```

A denial or ask contains a stable rule identifier and a safe reason. `ASK`
enters the bounded Host approval gate; it is not an Execution Decision and
receives no Handle or Scheduler entry.

A final allow-only `ExecutionDecision` contains the exact
`CommandParseResult`, policy rule identifier, and optional one-time approval
request identifier. It is the immutable authorization boundary: the execution
plane must perform that exact decision and must not re-parse, substitute, or
silently rewrite its command. A Decision for command A cannot be reused to
execute command B.

### Execution

Reuse the existing `Execution` domain object rather than introducing a
duplicate `Task` abstraction. An Execution begins only after an allowed
Decision is admitted and assigned an `exec_id`.

Execution continues to own the backend-neutral lifecycle:

- queued, running, and terminal state;
- bounded append-only output;
- stable Cursor reads;
- terminal exit information;
- cancellation and Session-release behavior.

Driver-specific resources remain private. A Shell Driver Execution may own a
process group or perform a Runtime-local builtin with no external resource; a
Tool Driver may own a worker invocation; an atomic managed command may have no
long-lived process to terminate.

## Control plane

The control plane is read-only with respect to the requested operation until
authorization succeeds. Its pipeline is:

```text
parse -> evaluate -> optional Host approval -> decide -> route -> execute
```

`parse` produces `CommandParseResult`; evaluation produces `ALLOW`, `DENY`, or
`ASK`; only an allowed evaluation or Host-approved ask produces the immutable
`ExecutionDecision` that may be routed and admitted.

### Command routing

Routing selects an internal command category:

- ordinary commands use the Shell driver;
- reserved `tools` commands later use a Tool driver.

Milestone 03 does not require a general routing registry. With only the Shell
driver present, routing may be trivial while retaining the Decision boundary.

Drivers are a closed, private Kernel seam. Workspaces and capabilities cannot
register new drivers or bypass policy.

### Execution Policy

Policy is supplied or configured by the Host and enforced at the Kernel
admission boundary. The Agent and Workspace may observe a safe denial reason
but cannot modify or relax the effective policy.

The effective policy is selected when the Runtime opens and remains fixed for
that Runtime lifetime. The Runtime default asks for a narrow set of recognized
direct filesystem mutators and otherwise allows. An embedding Host may
deliberately replace that policy at open time; neither an Agent request nor a
Workspace mutation can do so.

Conceptually:

```python
class ExecutionPolicy(Protocol):
    async def evaluate(
        self,
        command: CommandParseResult,
    ) -> PolicyEvaluation: ...
```

The Policy evaluation and Host approver interfaces are asynchronous. Approval
has a Runtime-wide active capacity and finite timeout and remains outside
Execution admission.

Policy rules should target stable operation facts where available:

```text
shell.execute
tool.list
tool.inspect
tool.run
```

Rules may later constrain executable name, Tool name, Capability Provenance,
Managed Path, network profile, or other planned capabilities.

### Admission invariants

1. No requested side effect occurs before `ALLOW`.
2. `DENY` and unresolved or rejected `ASK` return `policy_denied` without an
   `exec_id` or queue entry.
3. A policy error fails closed and emits a safe internal diagnostic.
4. Policy and approval never silently rewrite the requested command.
5. The admitted Decision is the same Decision passed to the execution plane.
6. Every driver is reachable only after the common policy gate.
7. Session close, Runtime close, cancellation, and forced cleanup never require
   policy approval.
8. Effective policy is Host-owned and snapshotted for a defined lifecycle; a
   Workspace write cannot expand current authority.

## Execution plane

The execution plane accepts only allowed, admitted, immutable Decisions.

### Execution supervision

The Session Kernel owns:

- Execution creation and state transitions;
- Session Scheduler integration and Driver lane release;
- wait behavior;
- output Buffer and Cursor coordination;
- driver start and cancellation;
- terminal snapshots;
- Session and Runtime cleanup.

The selected Driver synchronously prepares a side-effect-free Driver Execution
when scheduled work becomes runnable. The Kernel then uses only its common
`run` and `cancel` contract. It does not branch on command operation, Driver
kind, subprocess state, or future worker state.

`output` and `kill` address this state through a Session-private Handle. They do
not rerun command policy:

- `output` is authorized by possession of a Handle in the bound Session;
- `kill` may only reduce activity and must remain available for cleanup;
- a foreign or nonexistent Handle retains the same not-found behavior.

### Drivers

Drivers execute allowed Decisions but do not decide whether commands are
allowed:

| Driver | Prepared Execution | Owned execution resource |
|---|---|---|
| Shell | inline builtin or child process | none or process group |
| Tool | inline operation, process, or worker invocation | mechanism-specific |
| Managed command | Runtime-local operation | optimistic operation state |

Scheduling is Driver-aware without becoming Driver-visible to the model. The
Shell lane has capacity one per Session. A future Tool lane may run multiple
Executions within a bounded Host-configured budget. Admission remains ordered
and FIFO within each lane, while a runnable item may bypass an earlier item
that is blocked only on another lane. The accepted
[Driver-aware per-Session Execution Scheduler](./driver-aware-execution-scheduler.md)
discussion defines the detailed ordering and lifecycle semantics.

All Driver Executions emit normalized stdout/stderr-style output through the
shared bounded sink and return one backend-neutral terminal outcome.
Driver-specific fields do not leak into the Execution State or
`ExecutionSnapshot`.

MCP Tools continue through the Tool driver. They do not introduce an
MCP-specific execution or permission path.

## Executable-name policy

The first policy is deliberately small. It evaluates positively recognized
direct invocations using disjoint Host-configured allow, deny, and ask sets
plus one default action. The built-in ask set includes:

```text
chmod  chown  cp  dd  install  ln  mkdir  mv
patch  rm  rmdir  tee  touch  truncate  unlink
```

Additional names such as `rmdir` or `unlink` may be configured after their
desired compatibility impact is decided.

Minimum examples:

```text
rm file                  -> ASK
rm -rf build             -> ASK
/bin/rm file             -> ASK
pytest -q                -> ALLOW
```

The milestone 03 inspector used POSIX `shlex` tokenization and examined only
the first token of the submitted command. RFC-0001 replaced immediate denial
with `ALLOW`, `DENY`, and `ASK`; RFC-0002 additionally asks by default for
explicit file output redirection and in-place `sed`. Leading whitespace,
quoting of the executable name, and absolute executable paths remain covered
for configured basename rules.

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
    -> decides whether a parsed command may start

Sandbox
    -> limits what an allowed Decision can affect at runtime
```

The first version adds only authorization. Shell commands and executable Tools
retain the filesystem, network, process, and system authority of the embedding
Host process.

A future sandbox belongs in the execution plane:

```text
ExecutionDecision.constraints.sandbox_profile
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

- parse validated `exec` arguments into one Shell-only `CommandParseResult`;
- call the immediate `ExecutionPolicy.evaluate`;
- return `policy_denied` for recognized denied commands;
- return an allowed immutable `ExecutionDecision`;
- pass only that Decision to the execution plane.

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

- admit allowed Decisions into a bounded Scheduler in the bound Session;
- preserve at-most-one-running Shell Execution per Session;
- establish ordered, lane-aware scheduling without adding the future Tool
  Driver;
- let different Sessions execute concurrently;
- keep Handle lookup and cleanup Session-private;
- ensure policy denial consumes no queue capacity.

### Milestone 05: Workspace and Session environment

- establish `.workspace` idempotently during Runtime open and load the
  user-maintained `.workspace/env` configuration once per Runtime;
- initialize each Session Kernel with an independent copy of those Workspace
  values;
- recognize the narrow top-level `export KEY=VALUE ...` grammar as ordered
  Shell-lane Session mutation without adding a model-visible syscall;
- start each Shell child with `dict(os.environ) | session.env`, allowing
  Session values to override same-named Host values;
- bind the child environment when the Shell Execution starts rather than
  carrying environment values through policy facts or diagnostics;
- explicitly document that complete Host environment inheritance, including
  Provider credentials, is an accepted AEP-compatibility trade-off rather than
  a Secret-isolation guarantee.

### Milestone 06: Host-mediated execution approval

- evaluate `ALLOW`, `DENY`, or `ASK`;
- resolve `ASK` through a bounded Runtime-wide Host approver;
- keep unresolved approvals outside Execution and Scheduler capacity;
- cancel Session-owned approval waits during Session close;
- provide an allow-once Reference CLI prompt;
- leave ordinary Agent file mutations as Shell commands without optimistic
  conflict guarantees.

### Milestone 10: Tool commands

- define structured `tools list`, `tools info`, and `tools run` routes;
- expose Tool name, operation, validation state, and trusted provenance to
  policy;
- keep Tool dependency and process mechanics in the Tool driver;
- add a bounded parallel Tool lane without allowing Agent-authored metadata to
  grant concurrency authority;
- preserve model-returned Tool Result order when Tool work completes out of
  order;
- preserve one common Execution and permission path for generated MCP Tools.

### Later work

Possible later extensions:

- structured Shell AST and wrapper analysis;
- risk categories;
- immutable approval records;
- sandbox profiles and resource limits;
- semantic failure categories;
- Host-visible policy and execution diagnostics;
- persisted output Artifacts and retention;
- event-driven completion notifications.

## Historical acceptance criteria for the immediate-only first version

1. Every `exec` request passes the common policy gate before process creation.
2. A recognized direct `rm` invocation returns `policy_denied`.
3. A denied request creates no process, Handle, Execution State, or queue item.
4. An allowed request is executed from the same immutable Decision that policy
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

1. Which Host-facing diagnostic surface records policy decisions without
   persisting secrets?
2. What reserved CLI namespace will distinguish Kernel commands from host
   executables?
3. Which failure facts belong in canonical output, and which require a future
   backend-neutral result field?

## Relationship to unified execution dispatch

The companion
[Unified Execution dispatch](./unified-execution-dispatch.md)
discussion defines the CLI-first command surface, backend-neutral Execution,
and private execution drivers.

This proposal adds the missing admission boundary:

```text
CLI command
    -> CommandParseResult: what syntax facts are established?
    -> ExecutionDecision: may this exact command continue?
    -> ExecutionRoute: which trusted lane and Driver own it?
    -> Execution: how is it observed and managed?
    -> Driver Execution: how is it run and cancelled?
```

Together, the two discussions establish one path for Shell, Tool, and managed
Workspace commands without confusing command authorization with OS isolation.
