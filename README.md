<div align="center">

<img src="assets/cli-agent-logo.png" width="720" alt="cli-agent"/>

**English** · [中文](README_zh.md)

</div>

---

`cli-agent` is a general-purpose Agent that completes tasks through CLI
operations. It binds to a Workspace directory, inspects the state, runs
commands and Tools, and iterates until the task is done — either in an
interactive Session or as a one-shot task.

**Everything is a command.** The model is exposed only three Tools — `exec`
runs a command, `output` re-reads an Execution's retained output, and `kill`
terminates it. Everything else is expressed through one reserved command
grammar: file edits via `files write` / `files edit`, Tools via
`tools list` / `tools info` / `tools run`, session state via `cd` and `export`.
All capabilities (tools / skills / library / MCP) are discovered uniformly from
the directory-based Capability View — Repertoire as the lower layer, Workspace
as the upper layer with shadowing and copy-up — and add no model schemas, so
the three-Tool surface stays stable no matter how many capabilities are
installed.

## Highlights

| Feature | What it gives you |
|---|---|
| **Four-tier context compaction** | Long Sessions stay inside the model's context window: stale Tool Results are snipped, pruned, and old turns summarized; the Active Turn and user instructions are never truncated. |
| **Multi-backend support** | Provider-neutral model interface (OpenAI-compatible endpoints, scripted providers) and pluggable execution Backends behind a single Backend contract. |
| **Decoupled permission** | `ExecutionPolicy` is an optional Host-injected plugin deciding `ALLOW` / `DENY` / `ASK`; user interaction is a separate Host-owned channel. Everything fails closed. |
| **Everything is a command** | Only three model Tools (`exec` / `output` / `kill`); files, Tools and session state are all commands, and every capability is discovered from one unified directory-based catalog. |
| **Parallel scheduling** | Consecutive parallel-safe commands run in concurrent batches. |
| **Workspace-scoped environment** | Persistent `.workspace/env` snapshot plus per-Session exports that never leak across Sessions. |

## Architecture

![cli-agent architecture](assets/cli-agent-architecture.png)

cli-agent is layered from the Host down to the Backend:

- **Host / CLI** — `cli.py`, `config.py` and `runner.py` validate configuration
  and present events; `UserInteraction` is the Host-owned question channel.
- **AgentRuntime** — one Workspace-scoped Runtime owns Sessions. Each Session
  binds a `ModelProvider` and an `AgentLoop`; the `ContextManager` runs the
  four-tier compaction pipeline before every model request.
- **EnvironmentKernel** — splits the **control plane** (what may run:
  Host-injected `ExecutionPolicy` → `ALLOW` / `DENY` / `ASK` → Router → Shell
  AST → `ExecutionState`) from the **execution plane** (how it runs:
  backend-neutral requests → pluggable Backend → Workspace filesystem through
  the Capability View).
- **Capabilities** — directory-based Catalogs expose Tools, Skills, Library and
  MCP bindings uniformly; they add no model schemas.

## Installation

Requires Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/).

### Global install — use `cli-agent` from any directory

```bash
cd cli-agent
./scripts/install.sh            # editable install + one-time config
# or manually:
uv tool install --editable .    # --editable: track this checkout, no reinstall needed
```

First run creates `~/.cli-agent/.env` (mode 600) from
`cli-agent.env.example`; fill in the Provider settings once:

```bash
# ~/.cli-agent/.env
CLI_AGENT_MODEL="your-model"
CLI_AGENT_BASE_URL="https://api.example.com/v1"
CLI_AGENT_API_KEY="sk-..."
```

Now launch from anywhere:

```bash
cd ~/some/unrelated/project
cli-agent "Inspect this project"
```

Precedence: real environment variables (direnv/`.envrc`, a shell `export`)
always win over `~/.cli-agent/.env`. Uninstall with
`uv tool uninstall cli-agent`; `pipx install .` works the same way.

### Local development (this repository)

```bash
uv sync
cp .envrc.example .envrc
# Edit .envrc: set CLI_AGENT_MODEL, CLI_AGENT_BASE_URL, CLI_AGENT_API_KEY.
direnv allow
```

[direnv](https://direnv.net/) loads the Provider configuration and activates
the uv-managed virtual environment. Any OpenAI-compatible endpoint works; set
`CLI_AGENT_CONTEXT_WINDOW` when your model is not in the built-in registry.

## Usage

Start an interactive Session — each non-empty input is a new turn in the same
conversation; exit with `:q`, EOF, or `Ctrl+C`:

```bash
cli-agent
```

The TTY input box uses Enter to submit and Ctrl+J to insert a newline. Piped
and redirected input keeps line-based mode.

Run a task in one turn:

```bash
cli-agent "Inspect the Workspace"
```

Select the Workspace and capability Repertoire explicitly:

```bash
cli-agent \
  --workspace ./path/to/workspace \
  --repertoire ./path/to/repertoire
```

- `--workspace` — the directory the Agent works in (default: current directory).
- `--repertoire` — the user-maintained capability lower tree
  (default: `~/.cli-agent/repertoire`).
