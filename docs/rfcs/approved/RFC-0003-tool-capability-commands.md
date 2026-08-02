---
rfc_id: RFC-0003
title: Tool Capability Commands and Isolated Tool Environment
status: SUPERSEDED
author: cli-agent maintainers
reviewers:
  - name: Project owner
    status: approved
created: 2026-07-30
last_updated: 2026-08-02
decision_date: 2026-07-30
related_prds: []
related_rfcs:
  - RFC-0001
  - RFC-0002
  - RFC-0007
---

# RFC-0003: Tool Capability Commands and Isolated Tool Environment

> Superseded by [RFC-0007](../../proposed/RFC-0007-unified-command-routing-and-execution-refactor.md).
> This document remains as the historical Tool capability design. Its
> `ToolCommand` parser field, Tool Driver, Tool lane, and `parallel_tools`
> scheduling sections are no longer normative; current Tool execution uses
> the Custom command registry, Tool Catalog metadata, and the global Scheduler
> barrier model.

## Overview

This RFC adds the AEP `tools list`, `tools info`, and `tools run` command
surface to cli-agent. Runtime-open reconciliation builds a trusted Tool
Catalog from the Capability View, writes `index.md` as a derived projection,
and synchronizes one Workspace-private Python environment. Tool code runs in a
fresh worker process through the existing Execution lifecycle.

The initial default Policy allows every reserved `tools` invocation. Tool
validation still controls whether a catalog entry can be loaded. The
routing and scheduling model described below was later replaced by RFC-0007.

## Background & Context

### Current State

RFC-0002 presents Repertoire and Workspace Tool files at
`.workspace/tools`, but `tools` is not a registered custom command. AEP has
the desired user-facing grammar, while its current implementation reads
`index.md` as authority, stores a virtual environment inside the Tool tree,
and imports every Python file through a generated wrapper.

cli-agent already provides:

- Parser → Policy → Router → Scheduler → Driver execution;
- backend-neutral Execution snapshots, output, kill, and cleanup;
- trusted Capability View provenance;
- Host-owned scheduling configuration.

### Glossary

| Term | Definition |
|---|---|
| Tool Catalog | Runtime-open snapshot of Tool names, validation, documentation, paths, and actual Capability Provenance |
| Tool index | Runtime-generated `index.md` projection of the Tool Catalog; never an authority |
| Tool Environment | Workspace-private dependency state at `.workspace/.tool-environment/.venv` |
| Tool Driver | Runtime-private Driver that executes the reserved Tool grammar |
| Tool lane | Per-Session bounded scheduling lane independent of the Shell lane |

## Problem Statement

The Agent can see Tool files but cannot discover, inspect, or execute them
through the Runtime command protocol. Sending `tools ...` to a Host shell
would also permit a Host executable collision or unsupported Shell syntax to
bypass the intended Tool implementation.

If dependencies are installed in the Capability View or a shared
environment, Workspace evolution mutates user-maintained content or leaks
mutable state between Workspaces. Importing Tool modules into the Runtime
process would leak module state and weaken cancellation.

### Impact of Inaction

- Capability View Tools remain inert files.
- Tool calls cannot use existing Execution observation and cancellation.
- A future generated MCP Tool would need a separate execution path.
- Workspace dependencies and module state would lack isolation.

## Goals & Non-Goals

### Goals

1. Implement AEP-compatible list, info, quoted run, and `PY<< ... PY` run
   syntax.
2. Route every exact top-level `tools` command to Runtime code, including
   malformed pipelines and redirections.
3. Give Policy trusted operation, Tool reference, validation, and provenance
   facts.
4. Keep dependency state private to one Workspace and synchronize it at
   Runtime open.
5. Execute each run in a fresh cancellable worker process.
6. Let the Tool lane progress while the Shell lane is occupied.
7. Keep parallel authority exclusively in Host configuration.

### Non-Goals

1. Treat Tool code as safe merely because it validates or comes from the
   Repertoire.
2. Sandbox Tool filesystem, process, environment, or network access.
3. Detect whether a Workspace Tool was authored by a human or Agent.
4. Reload Tool files or dependencies during an already-open Runtime.
5. Add a model-visible syscall or an MCP-specific Driver.

### Success Criteria

- [x] List and info report trusted provenance and validation.
- [x] `index.md` is reproducible from the Catalog and is never read as
      authority.
- [x] Tool run works in a Workspace-private venv and never falls back to the
      Host interpreter.
- [x] Cancellation terminates the worker process group.
- [x] Fresh runs do not share module globals.
- [x] A running Shell does not alone block an admitted Tool execution.
- [x] Default Policy returns `ALLOW` for all reserved Tool operations.

## Evaluation Criteria

| Criterion | Weight | Description | Minimum Threshold |
|---|---:|---|---|
| AEP surface compatibility | High | Existing prompts can use the same command forms | Four documented forms |
| Trusted control facts | High | Policy and routing do not trust authored index metadata | Derived from filesystem and AST |
| Workspace isolation | High | Mutable dependency state is not shared | Distinct venv path per Workspace |
| Cancellation and cleanup | High | Tool work uses existing Execution ownership | Process-group termination |
| State isolation | High | Module globals do not cross runs | Fresh process per run |
| Maintainability | Medium | Tool execution reuses Driver contracts | No new model syscall |

## Options Analysis

### Option 1: Port AEP capability commands directly

**Description**

Read `tools/index.md`, create `tools/.venv`, and generate a wrapper that
imports every `*.py` file.

**Advantages**

- Smallest behavioral delta from the current AEP code.
- Low implementation effort.
- Existing AEP examples remain recognizable.

**Disadvantages**

- Authored `index.md` becomes a false authority for validation and scheduling.
- The venv is mixed into the merged capability tree.
- Runtime-generated wrapper quoting is difficult to audit.
- A single general scheduler lane can cause Tool/Shell head-of-line blocking.

**Evaluation**

| Criterion | Rating | Notes |
|---|---|---|
| AEP compatibility | Good | Directly matches current code |
| Trusted facts | Poor | Index contents are trusted |
| Workspace isolation | Limited | Per tree, but visible and shadowable |
| Cancellation | Adequate | Subprocess can be cancelled |
| State isolation | Good | One process per run |
| Maintainability | Limited | Generated source and mixed responsibilities |

**Effort**: S. **Risk**: authored metadata can influence trusted behavior.

### Option 2: Trusted Catalog plus private fresh workers

**Description**

Build an immutable Catalog from actual Capability View entries, generate the
index from it, place dependencies under private Workspace state, and send one
validated execution payload to a fixed worker for every run.

**Advantages**

- Policy receives actual provenance and structural validation.
- Capability files and dependency state have separate ownership.
- Fixed worker source avoids generated Python quoting.
- Fresh processes isolate module globals and reuse existing cancellation.
- A distinct Tool lane can progress independently of Shell.

**Disadvantages**

- Runtime open performs reconciliation work.
- Catalog facts remain an open-time snapshot until the next Runtime open.
- Arbitrary Python code can obscure statically referenced Tool names.
- Default-allow Tool policy deliberately permits broad code execution.

**Evaluation**

| Criterion | Rating | Notes |
|---|---|---|
| AEP compatibility | Good | Same documented surface |
| Trusted facts | Good | Catalog is filesystem-derived |
| Workspace isolation | Good | Private venv and markers |
| Cancellation | Good | Existing process execution |
| State isolation | Good | Fresh worker |
| Maintainability | Good | Fixed parser, catalog, environment, and driver boundaries |

**Effort**: M. **Risk**: Tool code retains Host process privileges available
to child processes.

### Option 3: Persistent Tool worker service

**Description**

Start one long-lived worker per Workspace or Session and exchange structured
requests over an IPC channel.

**Advantages**

- Avoids Python startup cost per call.
- Enables worker-side caches.
- Could support richer structured protocols later.

**Disadvantages**

- Module globals and import state cross calls unless explicitly reset.
- Cancellation and crash recovery require a second lifecycle.
- Runtime close must drain IPC and worker state.
- Dependency changes require coordinated worker replacement.

**Evaluation**

| Criterion | Rating | Notes |
|---|---|---|
| AEP compatibility | Good | Surface can match |
| Trusted facts | Good | Can consume the Catalog |
| Workspace isolation | Good | Per-Workspace worker |
| Cancellation | Limited | Per-request cancellation needs IPC support |
| State isolation | Poor | Persistent imports |
| Maintainability | Poor | Adds service lifecycle and protocol |

**Effort**: L. **Risk**: stale or poisoned worker state affects later calls.

### Options Comparison Summary

| Criterion | Direct AEP port | Catalog + fresh workers | Persistent worker |
|---|---|---|---|
| AEP surface | Good | Good | Good |
| Trusted facts | Poor | Good | Good |
| Workspace isolation | Limited | Good | Good |
| Cancellation | Adequate | Good | Limited |
| Module-state isolation | Good | Good | Poor |
| Maintainability | Limited | Good | Poor |

## Recommendation

Adopt Option 2. It retains the AEP command surface while satisfying the
Runtime's existing provenance, Driver, and Session isolation contracts.

Accepted trade-offs:

1. Runtime-open snapshot: local Tool evolution is reconciled on the next open.
2. Process startup: correctness and cancellation are prioritized over warm
   module state.
3. Default allow: Tool code is initially admitted without approval, as
   explicitly selected by the project owner. Hosts may replace the Policy.
4. Static reference limits: dynamic access is marked unknown and never grants
   parallel scheduling.

## Technical Design

### Architecture

```text
Runtime open
  Capability View
       |
       +--> Tool Catalog --> generated tools/index.md
       |
       +--> requirements.txt --> .tool-environment/.venv

exec("tools ...")
  Parser + Tool classifier
       |
       v
  Policy (default tool.* = ALLOW)
       |
       v
  Router --> Tool lane --> Tool Driver
                            |
                            v
                    fresh venv worker
```

### Reserved Grammar

Supported forms are exact:

```text
tools list
tools info <name>
tools run "<python code>"
tools run PY<<
<python code>
PY
```

Single, double, and matching triple quotes are accepted for the quoted form.
The heredoc marker and terminator are exactly `PY<<` and `PY`, allowing only
horizontal whitespace around line boundaries.

An exact top-level command head whose parsed token is `tools` is reserved.
Unsupported arguments, pipelines, redirections, backgrounding, or malformed
quotes remain on the Tool route and fail without spawning a Shell. A Host
executable named `tools` can be addressed by an explicit path such as
`/usr/local/bin/tools`; wrappers and explicit paths remain ordinary Shell
commands.

### Tool Catalog

At Runtime open, the Catalog examines top-level `.workspace/tools/*.py`
entries. A valid Tool:

- has a non-keyword Python identifier as its filename stem;
- is a regular effective file with valid Capability View provenance;
- is UTF-8 Python source that parses with `ast`.

The Catalog records name, effective path, Repertoire or Workspace provenance,
shadow state, validation error, module docstring, and optional companion
`<name>.md` documentation. Imports are not executed during validation.

`index.md` is atomically written from these facts. Runtime code never reads it
to decide availability, provenance, Policy, or scheduling.

### Policy Facts

`CommandParseResult.tool` contains immutable trusted facts when the reserved
grammar is selected:

- operation: `list`, `inspect`, `run`, or `invalid`;
- syntax validation and error;
- inspect name, when present;
- statically referenced `tools.<name>` entries;
- reference validation and actual provenance;
- whether dynamic Tool access prevents complete reference discovery.

The default `ExecutablePolicy` allows every reserved Tool fact with a
`tool.<operation>.allow` rule. Validation errors fail later in the Tool Driver;
they are execution errors rather than Policy denials.

### Tool Environment

The private state is:

```text
.workspace/.tool-environment/
├── .venv/
├── effective-requirements.txt
└── requirements.sha256
```

The Runtime creates a standard-library venv without inheriting Host
site-packages. If the effective `.workspace/tools/requirements.txt` changes,
`uv pip sync` synchronizes the private environment. The physical uv cache may
be shared; venv state may not.

Creation or synchronization failure is fail-soft: Runtime open succeeds,
Catalog list/info remain usable, and run returns the stored environment error.
It never falls back to the Host Python interpreter.

### Worker

The Tool Driver starts the private venv Python with one fixed Runtime-owned
worker script. A JSON payload sent on standard input contains code, Workspace,
cwd, and the valid Catalog path mapping. The worker:

1. adds the effective Tool directory to `sys.path`;
2. loads only Catalog-valid modules into a `tools` namespace;
3. executes statements and prints a non-`None` final expression;
4. reports syntax/import/runtime failures on stderr with a nonzero exit code.

Every invocation receives a fresh process, so module globals cannot cross
calls. The shared `_ProcessExecution` owns output and process-group
termination.

### Historical scheduling model

Execution routes carry a Runtime-owned lane. Existing Shell and Custom work
uses the default lane; Tool work uses a separate Tool lane. Both lanes share
the pending admission bound but claim runnable work independently.

List and info are parallel-safe. Run is parallel-safe only when every
statically referenced Tool name is valid, the reference set is non-empty, no
dynamic access is present, and every name is in the Host's
`parallel_tools` allow list. All other runs are serial within the Tool lane,
while still independent of a busy Shell lane. The Tool lane has its own
Host-configured capacity.

## Security Considerations

| Threat | Impact | Likelihood | Mitigation |
|---|---|---|---|
| Tool performs arbitrary Host operations | High | High | Document default-allow trade-off; replace Policy or use external sandbox |
| Forged provenance or parallel metadata | High | Medium | Derive Catalog from Capability View; Host allow list only |
| Tool imports Runtime process state | High | Medium | Fresh isolated child process |
| Dependency environment shared across Workspaces | High | Low | Workspace-private venv |
| Unsupported `tools` syntax reaches Shell | High | Medium | Reserve top-level Tool route before Shell fallback |
| Requirements sync executes untrusted build logic | High | Medium | Runs in child package manager; external sandbox remains required |

Validation is structural, not a trust or safety certification.

## Implementation Plan

### Phase 1: Catalog and grammar

- Add Catalog validation, documentation, provenance, and generated index.
- Add structured Tool command facts and exact grammar tests.
- Default all reserved Tool operations to `ALLOW`.

### Phase 2: Environment and Driver

- Create and synchronize the private Tool Environment at Runtime open.
- Add the fixed worker and Tool Driver.
- Cover output, errors, imports, module isolation, cancellation, and no Host
  Python fallback.

### Phase 3: Scheduling and integration

- Add the independent bounded Tool lane and Host configuration.
- Update Runtime, system message, README, handoff, and milestone records.
- Run the complete regression and lint suites.

### Rollback Strategy

Unregistering the Tool classifier and Driver restores the previous Shell-only
behavior. `.workspace/.tool-environment` and generated `tools/index.md` are
identifiable Runtime-owned state; rollback must not delete other Tool files.

## Open Questions

No decision-blocking questions remain. A future RFC may define trustworthy
human-versus-Agent authorship and a stricter default Tool Policy.

## Decision Record

**Status**: SUPERSEDED by RFC-0007

**Date**: 2026-07-30

**Approver**: Project owner

### Decision Summary

Implement AEP-compatible capability commands with a trusted Catalog,
Workspace-private dependency environment, fresh workers, and a distinct Tool
lane. Default every Tool invocation to `ALLOW`.

### Conditions

- Tool-authored files cannot grant parallel authority.
- Validation does not imply safety.
- Code remains uncommitted until peer review.

### Dissenting Opinions

None recorded.

## References

- `docs/rfcs/approved/RFC-0001-host-mediated-execution-approval.md`
- `docs/rfcs/approved/RFC-0002-workspace-capability-view.md`
- `../.scratch/cli-agent-runtime/issues/10-run-tools-in-an-isolated-environment.md`
- `/Users/huangzhenghao/Code/Python/Agent-Environment-Protocol/src/aep/runtime/session/capability_commands.py`
- `../CONTEXT.md`
