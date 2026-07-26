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

Run tests or start cli-agent from the Workspace directory:

```bash
pytest
cli-agent "Inspect the Workspace"
```

The current directory is the default Workspace. When starting the CLI from
elsewhere, make the environment and Workspace selection explicit:

```bash
direnv exec ./path/to/workspace \
  cli-agent "Inspect the Workspace" \
  --workspace ./path/to/workspace
```

direnv is a host-side environment loader, not a Runtime dependency.
