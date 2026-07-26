# cli-agent

`cli-agent` is a general-purpose Agent that completes tasks through CLI
operations.

## Environment

[direnv](https://direnv.net/) is the primary way to prepare Provider
credentials for cli-agent. The repository's `.envrc` loads an ignored
`.envrc.local` and activates the [uv](https://docs.astral.sh/uv/)-managed
virtual environment, so dependencies are reproducible and secret values do
not need to be committed.

```bash
uv sync
cp .envrc.local.example .envrc.local
# Edit .envrc.local and set the real Provider API key.
direnv allow
```

Run tests or start cli-agent from the Workspace directory:

```bash
pytest
cli-agent "Inspect the Workspace" --model <model>
```

The current directory is the default Workspace. When starting the CLI from
elsewhere, make the environment and Workspace selection explicit:

```bash
direnv exec ./path/to/workspace \
  cli-agent "Inspect the Workspace" \
  --workspace ./path/to/workspace \
  --model <model>
```

direnv is a host-side environment loader, not a Runtime dependency. cli-agent
reads the Provider key from `OPENAI_API_KEY` by default; `--api-key-env` can
select another variable.
