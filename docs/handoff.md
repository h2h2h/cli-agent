# Session handoff

Updated: 2026-07-27

## Completed

- Finished milestone 02, including the real OpenAI-compatible Provider path,
  one-shot Agent execution, terminal presentation, and offline CLI acceptance
  test.
- Consolidated the Python package under `src/cli_agent`; the public Runtime is
  imported from `cli_agent.runtime`.
- Added the `cli-agent` entry point and split configuration, running, and
  presentation into separate modules.
- Added an interactive Reference CLI mode that reuses one Runtime Session
  across multiple turns and exits through `:q`, EOF, or `Ctrl+C`, while
  preserving the positional one-shot mode.
- Improved terminal presentation with TTY-only color, concrete `exec` command
  diagnostics, and plain output when streams are redirected.
- Adopted uv for the virtual environment, dependency locking, tests, and Ruff.
- Adopted direnv as the configuration path. `.envrc` must export
  `CLI_AGENT_MODEL`, `CLI_AGENT_BASE_URL`, and `CLI_AGENT_API_KEY`.
- Kept `.envrc`, `AGENTS.md`, and `docs/` local and ignored by Git.

## Current state

- `cli-agent` starts a multi-turn interaction in one Session;
  `cli-agent "task"` runs one turn. Both paths can call a real compatible model,
  execute a short command through the built-in tools, continue with its Tool
  Result, and stream the final response.
- The full suite has 84 passing tests. `uv sync --locked --check`, Ruff lint,
  and Ruff format checks pass.
- The interactive path is proven offline through the real Provider Adapter,
  including Conversation History carried into a second turn.
- `main` is clean at commit `999f69c`.

## Known limits

- Conversation History is in memory for the active Session and is not restored
  after the CLI exits.
- `exec` parses `wait_ms` but still waits for the process and both output
  streams to finish before recording the Execution. Consequently, `output` and
  `kill` can only inspect an already completed record; they cannot observe or
  control a running command.
- `Ctrl+C` exits the Reference CLI with status 130, but complete process-group
  cleanup for an active command belongs to milestone 03.
- Commands currently inherit the complete cli-agent process environment,
  including direnv-loaded Provider credentials. Explicit Workspace environment
  requests and host grants are planned for milestone 05.

## Next

Start milestone 03, `support-long-running-executions`, from
`../.scratch/cli-agent-runtime/issues/03-support-long-running-executions.md`.
Keep the model-visible `exec`, `output`, and `kill` schemas fixed while moving
Execution ownership into the Environment Session so a timed-out command remains
addressable by its handle, cursor, and process group.
