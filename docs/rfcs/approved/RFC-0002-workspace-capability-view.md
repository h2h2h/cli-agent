---
rfc_id: RFC-0002
title: Workspace capability view over a default repertoire
status: IN_PROGRESS
author: cli-agent maintainers
reviewers:
  - name: project owner
    status: approved
created: 2026-07-30
last_updated: 2026-07-30
decision_date: 2026-07-30
related_prds: []
related_rfcs:
  - RFC-0001-host-mediated-execution-approval.md
---

# RFC-0002: Workspace capability view over a default repertoire

## Overview

This RFC implements milestone 07 as a filesystem-first Capability View inside
`.workspace`. A user-maintained Repertoire supplies the lower files; ordinary
Workspace files supply the writable upper. Lower files appear as file-level
symbolic links, while approved modifying Shell commands copy lower-backed
targets into the Workspace before execution.

The default Repertoire is
`~/.cli-agent/repertoire`. A Host may select another path when opening
the Runtime. Removing a lower-only file creates a persistent whiteout; removing
a Workspace override reveals the lower file again.

## Background & Context

### Current State

Runtime open creates `.workspace/env`. The Execution Policy can return
`ALLOW`, `DENY`, or `ASK`, but the Runtime has no Repertoire input, Capability
View, copy-up, whiteout, or trusted layer inspection.

AEP attaches one Profile by linking complete capability directories into a
Workspace. That provides filesystem visibility but directs writes back to the
Profile and therefore does not provide a read-only lower layer.

### Historical Context

Milestone 06 originally expected Agent-visible managed write commands.
RFC-0001 replaced those commands with Host-mediated approval of ordinary CLI
commands. The project owner subsequently selected file-level symbolic links,
approved-command copy-up, `.workspace` as the visible Capability View, a
default per-user Repertoire, and explicit whiteout semantics.

### Glossary

| Term | Definition |
|---|---|
| Repertoire | User-maintained lower tree containing `tools`, `skills`, and `library`. |
| Capability View | The effective filesystem tree under `.workspace`. |
| Workspace override | A real Workspace file at the same relative path as a lower file. |
| Whiteout | Runtime-owned metadata that keeps a lower-only path hidden across opens. |

## Problem Statement

### The Problem

The Agent and user need to inspect Repertoire capabilities as ordinary
Workspace files without allowing approved edits through the view to mutate the
Repertoire. Workspace-created files and overrides must persist, and removal
must distinguish hiding lower-only files from removing local overrides.

### Evidence

- Milestone 07 requires lower visibility, file-level copy-up, whiteouts,
  authoritative invalid overrides, and trusted provenance.
- A directory symbolic link sends both file creation and modification to its
  target.
- The current default Policy recognizes direct mutator executables but does
  not ask for Shell output redirection.

### Impact of Inaction

- `tools`, Skills, and Library milestones have no effective filesystem source.
- Agent-created Tools cannot coexist with a user-maintained lower Repertoire.
- A direct AEP-style directory link could mutate user-maintained content.

## Goals & Non-Goals

### Goals

1. Create or validate the default or Host-selected Repertoire idempotently.
2. Present lower files under `.workspace/{tools,skills,library}`.
3. Preserve real Workspace files as authoritative upper entries.
4. Copy a recognized lower-backed mutation target before its Shell command
   starts.
5. Persist file-level whiteouts and expose trusted provenance and shadow facts.
6. Ask for Shell output redirection under the default Policy.

### Non-Goals

1. Provide an operating-system sandbox or intercept arbitrary runtime-computed
   filesystem syscalls.
2. Add Agent-visible Workspace mutation commands.
3. Implement `tools list`, `tools info`, `tools run`, Tool dependencies, or
   generated indexes.
4. Add directory whiteouts or transactional rollback of partially executed
   Shell commands.
5. Detect stale writes between concurrent Sessions.

### Success Criteria

- [x] Runtime open creates the three Repertoire and Workspace capability trees.
- [x] Lower files are visible through file-level links and never overwritten
      by copy-up.
- [x] Workspace files shadow same-path lower files across Runtime opens.
- [x] Approved direct modifications and output redirections copy up their
      lower-backed targets before Shell execution.
- [x] The agreed `rm` behavior persists across Runtime opens.
- [x] Inspection derives provenance from actual layer state, not file content.

## Evaluation Criteria

| Criterion | Weight | Description | Minimum Threshold |
|---|---:|---|---|
| Ordinary CLI visibility | High | Files work with `ls`, `cat`, editors, and Shell commands | Real Workspace paths |
| Lower preservation | High | Recognized view writes do not update Repertoire files | Copy before execution |
| Portability | High | No privileged or OS-specific mount dependency | Python and symbolic links |
| Local evolution | High | New and modified files persist in the Workspace | Survives reopen |
| Trusted provenance | High | Source is derived from filesystem layer state | No self-declaration |
| Complete syscall interception | Medium | Runtime-computed writes are redirected | Trade-off may be explicit |

## Options Analysis

### Option 1: Directory symbolic links

**Description**

Link `.workspace/tools`, `skills`, and `library` directly to the Repertoire.

**Advantages**

- Small implementation.
- Matches AEP's current attachment mechanism.

**Disadvantages**

- New and modified files are written into the Repertoire.
- No upper precedence or whiteouts.

**Evaluation**

| Criterion | Rating | Notes |
|---|---|---|
| Ordinary CLI visibility | Good | Normal filesystem paths |
| Lower preservation | Inadequate | Writes follow the directory link |
| Portability | Good | Standard symbolic links |
| Local evolution | Inadequate | No Workspace upper |
| Trusted provenance | Adequate | Link target can identify lower |
| Complete interception | Inadequate | No interception |

**Effort**: XS. **Risk**: direct mutation of user-maintained content.

### Option 2: Real directories with file-level lower links

**Description**

Create real capability directories in `.workspace`, link individual lower
files into them, and replace recognized mutation targets with atomic Workspace
copies after approval.

**Advantages**

- New files naturally reside in the Workspace.
- Lower files remain ordinary readable paths.
- Requires no privileged mount or external service.
- Real files provide persistent upper precedence.

**Disadvantages**

- Command inspection is a guardrail rather than complete syscall
  interception.
- Directory mutations and runtime-computed writes need conservative handling
  or later filesystem support.
- Whiteouts require Runtime-owned metadata.

**Evaluation**

| Criterion | Rating | Notes |
|---|---|---|
| Ordinary CLI visibility | Good | Real directories and file links |
| Lower preservation | Good for recognized writes | Copy-up precedes spawn |
| Portability | Good | Uses Python filesystem operations |
| Local evolution | Good | Real Workspace files persist |
| Trusted provenance | Good | Entry kind and exact lower target determine source |
| Complete interception | Limited | Explicitly not a sandbox |

**Effort**: M. **Risk**: unrecognized programs may follow a lower link.

### Option 3: Mounted union filesystem

**Description**

Use OverlayFS, FUSE, or a platform-specific union mount to intercept filesystem
operations.

**Advantages**

- Provides transparent copy-up and deletion semantics.
- Handles runtime-computed writes without command parsing.

**Disadvantages**

- Requires OS-specific dependencies, mount lifecycle, and failure handling.
- Linux OverlayFS is not a portable macOS implementation.
- Deployment and debugging complexity increases.

**Evaluation**

| Criterion | Rating | Notes |
|---|---|---|
| Ordinary CLI visibility | Good | Native merged mount |
| Lower preservation | Good | Filesystem performs copy-up |
| Portability | Poor | Platform-specific backends |
| Local evolution | Good | Native upper |
| Trusted provenance | Good | Layer metadata |
| Complete interception | Good | Filesystem boundary |

**Effort**: XL. **Risk**: unavailable or failed mounts can block Runtime open.

### Options Comparison Summary

| Criterion | Directory links | File links | Union mount |
|---|---|---|---|
| CLI visibility | Good | Good | Good |
| Lower preservation | Inadequate | Good for recognized writes | Good |
| Portability | Good | Good | Poor |
| Local evolution | Inadequate | Good | Good |
| Trusted provenance | Adequate | Good | Good |
| Complete interception | Inadequate | Limited | Good |

## Recommendation

Adopt Option 2. It meets the selected cross-platform, filesystem-first
requirements without introducing a mount dependency. The accepted trade-off is
that Policy and command inspection remain cooperative guardrails rather than a
security boundary.

Conditions:

- Capability directories are real directories; only lower files are linked.
- Copy-up occurs only after authorization and before Shell spawn.
- Runtime-managed paths reject traversal and symbolic-link intermediates.
- The Repertoire must not overlap `.workspace`; it may otherwise reside under
  an ancestor selected as the Workspace.
- Direct access to the selected Repertoire path is outside Capability View
  protection.

## Technical Design

### Architecture

```text
~/.cli-agent/repertoire/       <workspace>/.workspace/
├── tools/                       ---> ├── tools/
├── skills/       file links     ---> ├── skills/
└── library/                     ---> ├── library/
                                       ├── env
                                       └── .capability-view/whiteouts/

exec -> parse -> Policy -> optional Host approval
                            |
                            v
                    Capability View preparation
                    -> copy-up recognized targets
                            |
                            v
                         Shell spawn
```

### Runtime API

```python
AgentRuntime.open(
    workspace=workspace,
    repertoire=None,  # default: ~/.cli-agent/repertoire
    provider=provider,
)
```

The Reference CLI accepts `--repertoire PATH`. A missing selected Repertoire is
created with real `tools`, `skills`, and `library` directories.

### Attachment Rules

1. A real Workspace file or directory is authoritative.
2. An exact link from a view path to the same Repertoire path is lower-backed.
3. An absent path receives a lower link unless a whiteout exists.
4. A conflicting Workspace symbolic link is invalid and does not expose a
   lower fallback.
5. Removed lower files cause their generated dangling links to be removed on
   the next Runtime open.

### Mutation Rules

- Creation beneath a real view directory naturally creates a Workspace entry.
- A recognized modification copies a lower-backed file to a temporary sibling
  and atomically replaces the link before Shell spawn.
- Removing a lower-only link records a whiteout after the command removes it.
- Removing an upper override reveals the lower link after command completion.
- Removing a Workspace-only file leaves the path absent.
- Failed or killed commands are reconciled from observed filesystem state;
  completed copy-up is not rolled back.

### Inspection

Inspection accepts a managed relative capability path and returns:

- `repertoire` for an exact lower link;
- `workspace` for a real upper entry;
- `whiteout` for a hidden lower path;
- whether a Workspace entry shadows a same-path lower entry.

File contents never determine provenance.

## Security Considerations

| Threat | Impact | Likelihood | Mitigation |
|---|---|---|---|
| Traversal or symlink escape in Runtime operations | High | Medium | Validate managed relative paths and exact link targets |
| Unrecognized program writes through a lower link | High | Medium | Document cooperative boundary; expand Policy inspection |
| Agent writes directly to known Repertoire absolute path | High | Medium | Keep path out of Agent context; external sandbox for strict containment |
| Stale or forged view link | Medium | Medium | Accept only exact Runtime-expected lower links |
| Concurrent copy-up | Medium | Medium | Runtime-wide lock and atomic sibling replacement |

Capability validation establishes structure and provenance. It does not certify
Tool code, Skill instructions, or Library content as safe.

## Implementation Plan

### Phase 1: Bootstrap and attachment

- Add Runtime and CLI Repertoire inputs.
- Create real Repertoire and Workspace trees.
- Attach lower files and preserve upper entries and whiteouts.

### Phase 2: Mutation lifecycle

- Ask for output redirection.
- Copy up recognized direct mutation targets before Shell spawn.
- Reconcile the agreed deletion behaviors after Shell completion.

### Phase 3: Inspection and conformance

- Add trusted path inspection.
- Cover reopen, shadowing, invalid overrides, concurrent copy-up, and lower
  preservation.
- Update README and handoff.

### Rollback Strategy

Removing the new Runtime integration leaves real Workspace files intact.
Runtime-created lower links and `.capability-view` metadata can be identified
without touching Repertoire content. Rollback must not delete real Workspace
overrides.

## Open Questions

No decision-blocking questions remain. Directory whiteouts, complete syscall
interception, and Tool/index behavior remain assigned to later work.

## Decision Record

**Status**: APPROVED

**Date**: 2026-07-30

**Approvers**:

- Project owner

### Decision Summary

Use `.workspace` as the Capability View, default the Repertoire to
`~/.cli-agent/repertoire`, attach lower content with file-level symbolic
links, copy up approved recognized writes, and use the agreed three-way `rm`
semantics.

### Key Discussion Points

1. Host approval remains necessary for modifying commands.
2. No new Mutation Plan or Agent-visible write abstraction is introduced.
3. Repertoire files are shadowed but never updated by Workspace overrides.

### Conditions of Approval

- Code is not committed before peer review.
- Policy classification remains separate from filesystem mutation.

### Dissenting Opinions

None recorded.

## References

- `docs/rfcs/approved/RFC-0001-host-mediated-execution-approval.md`
- `docs/handoff.md`
- `../.scratch/cli-agent-runtime/issues/07-mount-the-capability-view.md`
- `../CONTEXT.md`
