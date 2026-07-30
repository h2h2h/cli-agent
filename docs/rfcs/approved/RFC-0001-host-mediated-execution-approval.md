---
rfc_id: RFC-0001
title: Host-mediated execution approval
status: COMPLETED
author: cli-agent maintainers
reviewers:
  - name: project owner
    status: approved
created: 2026-07-30
last_updated: 2026-07-30
decision_date: 2026-07-30
related_prds: []
related_rfcs:
  - docs/discussions/control-plane-and-execution-plane.md
---

# RFC-0001: Host-mediated execution approval

## Overview

This RFC extends the Host-owned Execution Policy from immediate `ALLOW` or
`DENY` evaluation to `ALLOW`, `DENY`, or `ASK`. `ASK` delegates one exact
parsed command to a Host-provided approver before the command may become an
Execution. The embedded Runtime remains headless; the Reference CLI is one
possible approval user interface.

This decision replaces the planned Agent-visible `workspace read`, `workspace
write`, and `workspace remove` grammar. Ordinary Agent file operations remain
Shell commands. Runtime-owned filesystem operations may still use Managed
Paths, locking, and atomic replacement internally.

## Background & Context

### Current State

Every `exec` call is parsed into one immutable `CommandParseResult`. The
Runtime-owned Policy immediately returns an allow-or-deny
`ExecutionDecision`. Only an allowed Decision reaches custom-first routing,
Session admission, and Driver execution. The default rule denies recognized
direct invocations of `rm`.

Milestone 06 previously proposed reserved managed Workspace commands with
optimistic versions. That design combined two separate concerns:

- whether the Host authorizes an operation;
- whether cooperating file writers detect stale versions.

The project owner selected Host-mediated authorization and ordinary CLI file
operations instead of an Agent-specific write API.

### Glossary

| Term | Definition |
|---|---|
| Policy Evaluation | A read-only `ALLOW`, `DENY`, or `ASK` result for one exact parse result. |
| Execution Decision | The final immutable authorization accepted by the Execution Plane; it always means allow. |
| Approver | A Host-supplied asynchronous callback that resolves `ASK` to allow-once or deny. |
| Approval Request | A safe description of one exact parsed command and the Policy rule that requested review. |

## Problem Statement

### The Problem

Immediate allow-or-deny policy cannot pause a command for a user or Host
approval system. Encoding writes as reserved Runtime commands would make only
that grammar reviewable while ordinary Shell commands and future executable
Tools could still mutate files.

### Evidence

- The fixed model surface has only `exec`, `output`, and `kill`; adding an
  approval syscall would change the AEP-native contract.
- The current Parser can positively recognize a direct executable and generic
  Shell composition, but arbitrary programs can compute side effects at
  runtime.
- Multiple Session Kernels already share one Runtime-lifetime Policy, making
  the Host boundary the common enforcement point for Shell and future Custom
  commands.

### Impact of Inaction

- Hosts cannot require one-time human approval without replacing or wrapping
  the Runtime.
- Treating a managed write grammar as an approval mechanism leaves equivalent
  Shell writes outside that mechanism.
- Future Workspace Tools would need a second approval path.

## Goals & Non-Goals

### Goals

1. Represent `ALLOW`, `DENY`, and `ASK` without admitting unresolved work.
2. Let a Host approve or deny one exact command asynchronously.
3. Keep pending approvals outside Execution and Scheduler capacity.
4. Cancel a Session's pending approvals when that Session closes.
5. Bound concurrent Runtime approval requests.
6. Provide a Reference CLI allow-once prompt.

### Non-Goals

1. Detect every command or Tool that may mutate files.
2. Provide an operating-system sandbox or containment boundary.
3. Persist approval choices or add an approval history database.
4. Guarantee optimistic conflict detection for arbitrary Shell writes.
5. Implement Capability View copy-up or whiteouts.
6. Expose `workspace read`, `workspace write`, or `workspace remove`.

### Success Criteria

- [x] Only a final allowed `ExecutionDecision` reaches routing or admission.
- [x] Denied and unresolved requests create no Handle, Execution, or queue item.
- [x] Approval timeout, callback failure, invalid response, and absent approver
      fail closed.
- [x] Runtime and Session close cancel the relevant approval waits.
- [x] Reference CLI users can approve or deny a recognized `ASK` command once.
- [x] Tests document the direct-command guardrail's wrapper and dynamic-code
      limits.

## Evaluation Criteria

| Criterion | Weight | Description | Minimum Threshold |
|---|---:|---|---|
| Unified authorization | High | Applies before Shell and Custom routing | One common boundary |
| Exact-command integrity | High | Approval cannot authorize rewritten work | Immutable parse equality |
| Headless embedding | High | Runtime owns no terminal UI | Host callback |
| Resource bounds | High | Approval waits cannot grow without limit | Configured finite capacity |
| CLI compatibility | Medium | Agent continues using ordinary commands | No new model syscall |
| Conflict detection | Medium | Detects stale cooperating writes | Trade-off may be explicit |

## Options Analysis

### Option 1: Keep immediate ALLOW or DENY

**Advantages**

- Retains the smallest Policy and Kernel lifecycle.
- Adds no blocking Host interaction.
- Preserves current Runtime behavior.

**Disadvantages**

- Cannot implement user review.
- Hosts must pre-classify every decision.
- Future Tool review needs another mechanism or a breaking change.

**Evaluation**

| Criterion | Rating | Notes |
|---|---|---|
| Unified authorization | Adequate | Common boundary, but no review |
| Exact-command integrity | Good | Existing immutable binding |
| Headless embedding | Good | No UI |
| Resource bounds | Good | No approval resources |
| CLI compatibility | Good | No change |
| Conflict detection | Inadequate | Not addressed |

**Effort**: XS.

**Risk**: Hosts may execute commands that required contextual human judgment.

### Option 2: Add Agent-visible managed Workspace commands

**Advantages**

- Structured paths and versions support conflict-aware cooperating writes.
- Policy can authorize typed read, write, and remove facts.
- Runtime can validate Managed Paths before acting.

**Disadvantages**

- Introduces a second file-editing grammar beside normal CLI tools.
- Ordinary Shell and executable Tool writes bypass its guarantees.
- Does not itself provide a Host approval interaction.

**Evaluation**

| Criterion | Rating | Notes |
|---|---|---|
| Unified authorization | Poor | Covers only managed grammar |
| Exact-command integrity | Good | Structured operation is immutable |
| Headless embedding | Adequate | Still needs approval design |
| Resource bounds | Adequate | Execution bounds apply |
| CLI compatibility | Poor | Agent-specific editing API |
| Conflict detection | Good | Cooperating writes detect staleness |

**Effort**: M.

**Risk**: Documentation may imply protection that arbitrary Shell writes do
not receive.

### Option 3: Add Host-mediated ASK

**Advantages**

- Uses the same Policy boundary for Shell and future Custom commands.
- Keeps approval UI and organizational policy in the Host.
- Preserves the fixed model-visible syscall set and ordinary CLI operations.

**Disadvantages**

- Human latency can block an `exec` call.
- Approval requests require cancellation, timeout, and capacity ownership.
- Shell effect classification remains incomplete.
- Arbitrary Shell writes do not gain optimistic conflict detection.

**Evaluation**

| Criterion | Rating | Notes |
|---|---|---|
| Unified authorization | Good | ASK occurs before all routing |
| Exact-command integrity | Good | Approval resolves one parse result |
| Headless embedding | Good | Host callback owns interaction |
| Resource bounds | Good | Separate bounded approval gate |
| CLI compatibility | Good | No Agent-specific write grammar |
| Conflict detection | Poor | Explicitly deferred |

**Effort**: M.

**Risk**: A Host may overstate the modifying-command classifier. Documentation
and tests must retain the guardrail limitation.

### Options Comparison Summary

| Criterion | Immediate only | Managed commands | Host ASK |
|---|---|---|---|
| Unified authorization | Adequate | Poor | Good |
| Exact-command integrity | Good | Good | Good |
| Headless embedding | Good | Adequate | Good |
| Resource bounds | Good | Adequate | Good |
| CLI compatibility | Good | Poor | Good |
| Conflict detection | Inadequate | Good | Poor |

## Recommendation

Adopt Option 3. It is the only evaluated option that provides human review at
the common control-plane boundary while preserving the CLI-shaped environment.

Accepted trade-offs:

1. Ordinary Shell writes may silently overwrite concurrent changes.
2. Executable-name rules and Shell syntax facts are guardrails, not complete
   side-effect classification.
3. Capability View copy-up requires a separate milestone 07 design.

## Technical Design

### Architecture

```text
exec(command)
    -> parse
    -> ExecutionPolicy.evaluate
         ALLOW --------------------------+
         DENY -> policy_denied           |
         ASK  -> bounded Host approver --+--> final ExecutionDecision
                                                   |
                                                   v
                                      Router -> Scheduler -> Driver
```

An unresolved `ASK` is not an Execution Decision. The Execution Plane accepts
only the final allow-only `ExecutionDecision`.

### Policy Model

`PolicyEvaluation` contains the exact `CommandParseResult`, one action, a
stable rule identifier, and a safe reason for `DENY` or `ASK`.

The first configurable executable Policy supports disjoint allow, deny, and
ask basename sets plus a default action. The built-in default asks for direct
`chmod`, `chown`, `cp`, `dd`, `install`, `ln`, `mkdir`, `mv`, `patch`, `rm`,
`rmdir`, `tee`, `touch`, `truncate`, and `unlink`, and otherwise allows. This
preserves a narrow positive-recognition guardrail without claiming
comprehensive mutation detection.

### Approval Model

`ExecutionApprover.approve(request)` returns `ALLOW` or `DENY`. The request
contains an opaque request ID, Host-visible Session ID when available, raw
command, parsed tokens, executable basename, Shell-composition fact, Policy
rule, and safe reason.

The approval gate:

- enforces one Runtime-wide finite active-request capacity;
- applies a finite timeout;
- validates the response type;
- converts callback failure into a safe denial;
- never permits command replacement.

Every Session Kernel tracks only its own approval tasks. Session close cancels
those tasks; Runtime close closes every Kernel and therefore every pending
approval.

### Reference CLI

The CLI prints the command and reason to stderr and reads one response from
stdin. Only `y` or `yes`, case-insensitively, allows the command once. EOF and
all other responses deny it. The CLI does not persist decisions.

## Security Considerations

| Threat | Impact | Likelihood | Mitigation |
|---|---|---|---|
| Approval reused for another command | High | Low | Result remains local to one immutable parse result |
| Approval flood | Medium | Medium | Runtime-wide finite active capacity |
| Approver hangs | Medium | Medium | Finite timeout and close cancellation |
| Callback fails open | High | Low | Exceptions and invalid responses deny |
| Wrapper bypasses basename rule | High | Medium | Document and test non-coverage; Host may use default ASK |
| Command text contains a secret | Medium | Medium | Request is Host-only; Runtime does not persist it |

This Policy is not an operating-system sandbox. An allowed process retains the
Host identity's filesystem, process, network, and environment access.

## Implementation Plan

1. Add the public Policy and approval data model.
2. Add the Runtime-wide bounded approval gate.
3. Integrate ASK resolution before routing and admission.
4. Expose Policy and approver configuration from `AgentRuntime.open`.
5. Add the Reference CLI allow-once approver.
6. Prove allow, deny, approve, rejection, failure, timeout, capacity, and close
   behavior.
7. Update handoff and superseded architecture statements.

Rollback consists of reverting this RFC's implementation and restoring the
previous direct-`rm` deny policy. No persisted state migration is required.

## Open Questions

1. Whether future Hosts need durable approval audit records.
2. Resolved by
   [RFC-0002](./RFC-0002-workspace-capability-view.md): the default Policy
   distinguishes explicit file output redirection and in-place `sed` editing.
3. Resolved by RFC-0002: `.workspace` uses real capability directories,
   file-level lower links, approved-command copy-up, and persistent whiteouts.

These questions do not block the first allow-once implementation.

## Decision Record

**Status**: APPROVED

On 2026-07-30 the project owner selected Host-mediated `ASK`, rejected
Agent-visible managed Workspace mutation commands, accepted the loss of
conflict-safe arbitrary Shell writes, and required human review policy to
remain Host-owned.

## References

- [Control plane and execution plane](../../discussions/control-plane-and-execution-plane.md)
- [AEP-aligned Custom dispatch and ordered parallel scheduling](../../discussions/aep-aligned-custom-dispatch-and-parallel-scheduling.md)
- [`CONTEXT.md`](../../../../CONTEXT.md)
