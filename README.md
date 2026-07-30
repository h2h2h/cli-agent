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
`~/.config/cli-agent/repertoire`. Select another user-maintained lower tree
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

## Execution approval

The default Runtime Policy asks the Host to approve recognized direct
filesystem-mutating commands:

```text
chmod  chown  cp  dd  install  ln  mkdir  mv
patch  rm  rmdir  tee  touch  truncate  unlink
```

Shell output redirection to a file and `sed -i` also require approval by
default. Input redirection, file-descriptor duplication, and ordinary
read-only `sed` invocations do not.

Other executable names are allowed by default. The Reference CLI displays the
exact command and reason on stderr:

```text
[approval] direct invocation of 'rm' requires Host approval
  command: rm report.txt
Allow once? [y/N]
```

Only `y` or `yes` allows that command once. Every other response, EOF, timeout,
missing approver, callback failure, or invalid response fails closed without
creating an Execution. An unresolved approval does not receive an `exec_id` or
consume Execution queue capacity.

Embedding Hosts can supply an `ExecutablePolicy` with disjoint allow, deny, and
ask executable-name sets, a default `PolicyAction`, and an asynchronous
`ExecutionApprover` through `AgentRuntime.open`. Executable-name inspection is
a narrow admission guardrail augmented by explicit output-redirection and
in-place-`sed` recognition. Wrappers, scripts, interpreters, compound commands,
and arbitrary programs can still perform effects that are not visible from
the first executable name. It is not an operating-system sandbox or
comprehensive modification detector.

## Capability View

Runtime open creates or validates the default or selected Repertoire:

```text
~/.config/cli-agent/repertoire/
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

After Policy and optional Host approval, recognized modifying Shell commands
copy a lower-backed target into the Workspace before the child process starts.
Removing a lower-only file creates a persistent whiteout; removing a Workspace
override reveals the lower file again; removing a Workspace-only file leaves
it absent. Runtime-owned inspection derives source and shadow facts from the
actual link and layer state rather than file-authored metadata.

This is a cooperative Capability View, not filesystem containment. A script or
interpreter whose runtime-computed write is not recognized can still follow a
lower link, and a command that directly addresses the external Repertoire path
bypasses the view. Use an external sandbox when strict lower immutability is
required.

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
