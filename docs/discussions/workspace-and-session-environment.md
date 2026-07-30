# Workspace and Session environment

Status: accepted for milestone 05

Updated: 2026-07-28

## Context

Milestones 01 through 04 let every Shell child inherit the embedding process
environment implicitly because the Shell Driver does not pass `env=`.
The previous milestone 05 design proposed a names-only Workspace Environment
Request, an explicit Host Environment Grant, a Runtime-controlled minimum, and
an immutable filtered environment snapshot.

The implementation session rejected that design as disproportionate to the
current product goal. The replacement intentionally follows AEP's custom
Session environment model while adding a persistent Workspace source.

## Evaluation criteria

- Keep Workspace setup and embedding APIs small.
- Preserve familiar AEP `export` behavior for serial Shell commands.
- Keep Session-local mutations isolated.
- Leave a stable persistent namespace for later capability and derived state.
- Preserve the fixed `exec`, `output`, and `kill` model surface.
- State Host-environment and Secret exposure accurately.

## Options considered

### Option A: request and explicit Host grant

The Workspace persists variable names only. The Host supplies values when the
Runtime opens, and Sessions receive the intersection plus a controlled
minimum.

Advantages:

- Child commands do not inherit unrelated Host variables.
- Provider credentials remain outside Agent execution unless explicitly
  granted.
- Runtime-open environment snapshots are deterministic.

Disadvantages:

- Requires two coordinated configuration surfaces.
- Adds Host API, diagnostics, snapshot ownership, and policy-data constraints.
- Does not provide persistent Session `export` without another state model.

### Option B: AEP-style Host inheritance with Workspace and Session overrides

Runtime open loads custom values from `.workspace/env`. Each Session receives
an independent copy, direct top-level `export` updates that copy, and each
Shell child starts with `dict(os.environ) | session.env`.

Advantages:

- Matches AEP's established environment behavior.
- Uses one Workspace configuration surface.
- Supports Session-local `export` with the existing serial Shell lane.
- Requires no Host Environment Grant API or controlled-minimum definition.

Disadvantages:

- Every child inherits all embedding-process variables.
- Provider credentials and other Host Secrets are available to Agent commands.
- Values stored under `.workspace/env` are readable Workspace data and can be
  committed accidentally.
- Host `os.environ` changes can alter later Executions in an active Runtime.

### Option C: Workspace-only environment

Runtime open loads `.workspace/env`, Sessions copy it, and child processes use
only Session values without inheriting the Host environment.

Advantages:

- One configuration surface and no ambient Host inheritance.
- Workspace behavior is more deterministic than option B.

Disadvantages:

- Users must reconstruct basic process variables explicitly.
- Diverges from AEP.
- Requires a portability contract for `PATH`, locale, temporary directories,
  and Windows process requirements.

## Decision

Adopt option B.

This decision supersedes the milestone 05 request/grant/filtering design.
The accepted child environment is:

```python
child_env = dict(os.environ) | session.env
```

The right-hand Session mapping wins on key collisions.

## Persistent Workspace namespace

`AgentRuntime.open` owns one idempotent Workspace bootstrap before it loads
configuration or creates Sessions:

1. Resolve and validate the existing Workspace root.
2. Create `.workspace` when absent.
3. Reject `.workspace` when it is a symbolic link or non-directory.
4. Create or validate the `.workspace/env` regular dotenv file.
5. Load the Workspace environment once.
6. Continue with later Runtime-open preparation such as Repertoire
   Reconciliation and Capability View attachment.

Runtime close never deletes `.workspace`. An open failure does not remove an
empty namespace it created. The Runtime does not overwrite user environment
files or modify `.gitignore`.

At milestone 05, `.workspace` was only a reserved persistent namespace and not
yet the Capability View.
[RFC-0002](../rfcs/approved/RFC-0002-workspace-capability-view.md) later
selected `.workspace/tools`, `.workspace/skills`, and `.workspace/library` as
the visible Capability View while retaining `.workspace/env` and hidden
Runtime-owned metadata in the same persistent namespace.

## Session semantics

- Runtime open loads one Workspace environment mapping from `.workspace/env`.
- Each new Session receives an independent copy.
- Closing and recreating a Session discards its exports and copies the same
  Runtime-open Workspace mapping again.
- A direct top-level `export KEY=VALUE ...` mutates only the current Session.
- Export mutation runs in the serial Shell lane. A later Shell Execution sees
  an earlier completed export.
- A nested export handled by a child shell is process-local and does not mutate
  Runtime state.
- Each Shell Execution merges the current embedding `os.environ` with the
  current Session mapping when it starts.
- Host environment changes therefore affect later Executions without a Runtime
  reopen. `.workspace/env` changes still require a later Runtime open.
- Future non-Shell lanes have no ordering guarantee relative to export until a
  later design introduces a cross-lane state barrier.

## Model surface

The model-visible syscall set remains exactly `exec`, `output`, and `kill`.
Top-level export and any later environment-inspection command are CLI-shaped
operations submitted through `exec`; no `aep_env` model Tool is added.

## Security consequences

This design intentionally does not provide environment-variable filtering:

- Provider configuration loaded through direnv is inherited by Shell children.
- `.workspace/env` values are visible to any Agent command that can read the
  Workspace.
- The Runtime does not redact values deliberately printed by commands.
- `.workspace` being hidden by naming convention is not an access-control
  boundary.

Documentation should recommend excluding environment values from version
control, but the Runtime must not silently edit user ignore files.

Secret References remain appropriate for Runtime-managed MCP and Provider
configuration. They no longer describe the general Shell child environment.

## On-disk Workspace environment convention

The format comparison is recorded in milestone issue 00. The accepted
convention is one regular dotenv file:

```text
.workspace/
└── env
```

```dotenv
API_BASE_URL=https://example.test/api
GITHUB_TOKEN="replace me"
REPORT_FORMAT=json
```

This replaces the earlier one-file-per-variable design. A standard dotenv file
is familiar, compact, and avoids using filenames as environment keys.
`python-dotenv` owns quoting and comment parsing instead of a Runtime-maintained
regular-expression grammar.

The normative rules are:

- `.workspace/env` is a real regular file; directories, symbolic links,
  sockets, devices, and other object types fail Runtime open;
- Runtime open creates an empty file when it is absent;
- the file is strict UTF-8 and uses `python-dotenv` syntax;
- blank lines, comments, quoted and multiline quoted values, and the optional
  `export` prefix are supported;
- every variable uses `KEY=VALUE`; a bare `KEY` is invalid and `KEY=` is empty;
- names are case-sensitive, values and names may not contain NUL, and the last
  duplicate assignment wins;
- interpolation is disabled, so `${NAME}` remains literal;
- any invalid line fails Runtime open before any mapping is published;
- diagnostics identify a line and path but never include configuration values;
- writers should atomically replace the complete file from a temporary sibling
  so each Runtime open sees one coherent dotenv snapshot.

There is no format autodetection, old-directory migration, or Runtime-managed
encryption.
