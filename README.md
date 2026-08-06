# cli-agent

`cli-agent` is a general-purpose Agent that completes tasks through CLI
operations.

## Environment

[direnv](https://direnv.net/) is the primary way to prepare Provider
configuration for cli-agent. Copy the repository template to the ignored
`.envrc`, then export the model, base URL, and API key there. The same file
activates the [uv](https://docs.astral.sh/uv/)-managed virtual environment.

```bash
uv sync
cp .envrc.example .envrc
# Edit .envrc and set CLI_AGENT_MODEL, CLI_AGENT_BASE_URL,
# and CLI_AGENT_API_KEY.
direnv allow
```

Run tests or start an interactive cli-agent Session from the Workspace
directory:

```bash
pytest
cli-agent
```

Each non-empty input is submitted as a new turn in the same Session, so the
Agent retains the conversation and Tool interactions until the process exits.
Enter `:q`, send EOF, or press `Ctrl+C` to exit.

Pass a task to run one turn without entering the interactive Session:

```bash
cli-agent "Inspect the Workspace"
```

The current directory is the default Workspace. When starting either mode from
elsewhere, make the environment and Workspace selection explicit:

```bash
direnv exec ./path/to/workspace \
  cli-agent \
  --workspace ./path/to/workspace
```

The default capability Repertoire is
`~/.cli-agent/repertoire`. Select another user-maintained lower tree
when needed:

```bash
cli-agent \
  --workspace ./path/to/workspace \
  --repertoire ./path/to/repertoire
```

direnv is a host-side environment loader, not a Runtime dependency.

Shell Executions inherit the complete environment of the embedding cli-agent
process. This includes the Provider variables loaded by direnv, so Agent
commands can inspect or emit `CLI_AGENT_API_KEY` and any other exported value.
Run the complete Runtime inside an external sandbox when that exposure is not
acceptable.

## Execution policy and user interaction

Policy is an optional Host-injected plugin: `execution_policy=None` fully skips
Policy evaluation and constructs no implicit decision. A configured
`ExecutionPolicy` evaluates every parsed, routed command with
`evaluate(ShellParseResult)` and returns `ALLOW`, `DENY`, or `ASK`.

- `DENY` blocks the current command with `policy_denied` and the Policy reason.
- `ASK` is converted by the Runtime into a standard question carrying the
  Policy reason and the exact command, with the fixed `allow_once` and `deny`
  options. The Reference CLI displays it on stderr:

```text
[interaction] direct invocation of 'rm' requires Host approval
command: rm report.txt
Allow once? [y/N]
```

Only `allow_once` allows that command once and never persists. `deny`,
cancellation, interaction failure, and invalid answers fail closed without
creating an Execution or consuming queue capacity. Policy exceptions and
invalid evaluations also fail closed, are reported through the Host
diagnostic, and leave the Session usable.

Embedding Hosts pass a required `user_interaction` and an optional
`execution_policy` to `AgentRuntime.open`:

```python
await AgentRuntime.open(
    workspace=...,
    provider=...,
    user_interaction=terminal_interaction,
    execution_policy=None,  # or any ExecutionPolicy implementation
)
```

The Runtime never owns or closes the Host interaction, and Session or Runtime
close only cancels pending asks. Executable-name inspection and
redirection recognition are narrow admission guardrails at best: wrappers,
scripts, interpreters, and compound commands can still perform effects that
are not visible from the first executable name. Policy and interaction are
plugin boundaries, not an operating-system sandbox or comprehensive
modification detector.

## Capability View

Runtime open creates or validates the default or selected Repertoire:

```text
~/.cli-agent/repertoire/
├── tools/
├── skills/
└── library/
```

It presents the effective capability files inside the Workspace:

```text
.workspace/
├── env
├── tools/
├── skills/
├── library/
└── .capability-view/
    └── whiteouts/
```

The three capability directories are real Workspace directories. Repertoire
files appear within them as file-level symbolic links, while Workspace-created
files are ordinary files. A Workspace file at the same relative path shadows
the Repertoire version.

After optional Policy evaluation, recognized modifying Shell commands copy a
lower-backed target into the Workspace before the child process starts.
Removing a lower-only file creates a persistent whiteout; removing a Workspace
override reveals the lower file again; removing a Workspace-only file leaves
it absent. Runtime-owned inspection derives source and shadow facts from the
actual link and layer state rather than file-authored metadata.

This is a cooperative Capability View, not filesystem containment. A script or
interpreter whose runtime-computed write is not recognized can still follow a
lower link, and a command that directly addresses the external Repertoire path
bypasses the view. Use an external sandbox when strict lower immutability is
required.

## Tool capability commands

Runtime open scans top-level Python files in `.workspace/tools` and generates
`.workspace/tools/index.md` as a readable projection. The index reports Tool
validation, actual Repertoire or Workspace provenance, shadowing, and a short
documentation summary. It is generated output: Policy, execution, and
scheduling use the Runtime's trusted open-time Tool Catalog rather than reading
claims from `index.md`.

Use the reserved AEP-compatible grammar through `exec`:

```text
tools list
tools info math_tool
tools run "tools.math_tool.add(2, 3)"
tools run <<'PY'
values = [tools.math_tool.add(1, 2), 4]
json.dumps(values)
PY
```

`tools run` accepts ordinary Python composition and prints a non-`None` final
expression. Each invocation starts a fresh worker with the Workspace-private
interpreter at `.workspace/.tool-environment/.venv`; Tool module globals do
not survive into a later invocation. Declare shared dependencies in
`.workspace/tools/requirements.txt`. Runtime open reconciles changed
requirements with `uv pip sync`, while uv's physical package cache may still
be shared.

Dependency synchronization failure is fail-soft: the Runtime can still open
and `tools list` / `tools info` remain available, but `tools run` reports that
the Tool Environment is unavailable. It never silently falls back to the Host
Python interpreter.

Every exact top-level `tools` command is Runtime-reserved. Unsupported
pipelines, redirections, backgrounding, or malformed arguments fail on the
Tool route rather than falling through to a Host executable. Use an explicit
path to invoke a Host program also named `tools`.

Without a configured execution Policy, all Tool invocations run without
Policy evaluation, including Workspace Tools and arbitrary Python payloads.
Tool code inherits the child-process environment and is not filesystem,
process, network, or Secret sandboxed. Embedding Hosts can inject an
execution Policy, and should use an external sandbox where this authority is
unacceptable.

One global Scheduler batches consecutive parallel-safe commands.
`AgentRuntime.open(parallel_commands=...)` authorizes executable basenames
whose direct Shell invocations may run in a parallel batch; `tools list` /
`tools info` and any Custom command may declare their own
`parallel_safe` fact. This authority comes only from Runtime configuration
and command metadata; Tool files and generated indexes cannot grant it.

## File mutation commands

`files write` and `files edit` are the Runtime-reserved channels for file
mutations. Both carry their payload through an exact heredoc, so
model-generated content is never re-quoted or expanded by a Shell:

```text
files write <path> <<'EOF'
<content>
EOF

files edit <path> <<'EDI'
{"edits": [{"oldText": "...", "newText": "..."}, ...]}
EDI
```

`files write` creates or overwrites a file, creating parent directories, with
an atomic replace that preserves the existing mode. `files edit` applies one
or more exact, unique, non-overlapping text replacements to a single file in
one call; UTF-8 BOM and CRLF line endings are preserved. Both commands prepare
Capability View paths before writing, so `.workspace/tools` and friends are
copied up rather than pierced through to the Repertoire. Relative paths
resolve against the current working directory and are not restricted to the
Workspace, matching `cd` semantics.

Every exact top-level `files` command is Runtime-reserved. Unknown
subcommands, missing heredocs, dynamic paths, invalid JSON, and unmatched or
duplicated `oldText` fail with a specific diagnostic on the `files` route and
never fall back to a Host executable. `files` commands always run serially
(`parallel_safe=False`). Use an explicit path to invoke a Host program also
named `files`.

## Workspace and Session environment

`.workspace/env` is a dotenv file containing persistent Workspace custom
environment values. Runtime open loads it once as an immutable Workspace
snapshot, and each new Session receives an independent mutable copy:

```dotenv
API_BASE_URL=https://example.test/api
GITHUB_TOKEN="replace me"
REPORT_FORMAT=json
```

The file uses strict UTF-8 and `python-dotenv` syntax. Blank lines, comments,
quoted values, multiline quoted values, and an optional `export` prefix are
supported. Use `KEY=` for an empty value; bare keys are invalid. Duplicate keys
use the last assignment, names are case-sensitive, and `${NAME}` is kept
literal rather than interpolated. The path must be a regular file, not a
directory or symbolic link.

Within a Session, a direct top-level command such as
`export REPORT_FORMAT=json` updates only that Session's in-memory custom
environment. Multiple `KEY=VALUE` assignments are atomic. Nested exports,
pipelines, compound shell expressions, and command substitutions remain
child-shell-local. Closing the Session discards its exports.

Each child Shell process starts with
`dict(os.environ) | session.env`. Session values override same-named Host
values, and later Host environment changes affect later Executions without
reopening the Runtime.

This file is Agent-readable Workspace data, not a Secret store. Exclude
`.workspace/env` from version control when it contains credentials.

## Context budget and compaction

Each Session manages its conversation against an explicit Context budget. The
model's maximum Context Window comes from the built-in model registry (for
example `deepseek-v4-flash` = 1M tokens); set `CLI_AGENT_CONTEXT_WINDOW` to
override it for an unregistered model or a custom endpoint limit:

```bash
# Optional overrides; defaults come from the model registry and built-in budgets.
# export CLI_AGENT_CONTEXT_WINDOW="128000"
# export CLI_AGENT_OUTPUT_RESERVE="16384"
# export CLI_AGENT_CONTEXT_SAFETY_MARGIN="4096"
```

`CLI_AGENT_OUTPUT_RESERVE` defaults to 16384 and `CLI_AGENT_CONTEXT_SAFETY_MARGIN`
to 4096. The input budget is `window - output_reserve - safety_margin`; a
non-positive budget fails fast at startup.

Before every normal model request the Runtime projects the next request's input
tokens and compacts old content with four tiers, without ever guessing a model
specification or truncating User instructions:

1. **Snip (60%)**: replace the oldest stale success Tool Results outside the
   Protected Suffix with a bounded head/tail placeholder that keeps `exec_id`,
   status, exit code, cursor, and a re-read hint (`output` can refetch retained
   output for the same execution).
2. **Prune (80%)**: reduce snipped results further to execution identification
   plus a reclaimed marker.
3. **Summarize (95%)**: merge the oldest completed turns into a structured
   summary with `## Progress` / `## Files` / `## Todo` / `## Context` sections,
   projected as delimited Assistant history data (never a second System
   message), through a dedicated no-tools model request.
4. **Oversized guard**: a single oversized but re-readable result in the
   current turn is compacted to restore the budget; unrecoverable input raises
   a stable `ContextOverflowError` instead of silently deleting content.

The Active Turn and the most recent complete turns are protected; Tool Calls
and Tool Results always stay paired by `call_id`. Reported Provider usage is
anchored per request revision; everything appended after it is conservatively
estimated and labeled `estimated`. When a Provider reports a Context Overflow,
the Runtime forces compaction and retries that model step exactly once (never
repeating Tool execution), then fails with a stable error.

Compaction progress is visible through `RuntimeDiagnostic` kinds such as
`context.snipped`, `context.pruned`, `context.summarized`,
`context.oversized_result`, and `context.compaction_failed`, without leaking
message bodies, command output, or Secrets.
