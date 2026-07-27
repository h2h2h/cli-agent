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
- Preserved the final v1 code state on `main` and the annotated
  `cli-agent-v1-baseline-2026-07-27` tag, both pushed to `origin`.
- Created the `v2` development branch and began versioning `AGENTS.md` and
  `docs/`; `.envrc` remains local and ignored.
- Recorded the proposed unified dispatch and control-plane/execution-plane
  architectures as non-normative discussions for the v2 reconciliation.

## Current state

- `cli-agent` starts a multi-turn interaction in one Session;
  `cli-agent "task"` runs one turn. Both paths can call a real compatible model,
  execute a short command through the built-in tools, continue with its Tool
  Result, and stream the final response.
- The full suite has 84 passing tests. `uv sync --locked --check`, Ruff lint,
  and Ruff format checks pass.
- The interactive path is proven offline through the real Provider Adapter,
  including Conversation History carried into a second turn.
- `v2` is the active development branch. `main` is the frozen v1 maintenance
  branch at tag `cli-agent-v1-baseline-2026-07-27`.
- The v1 tag deliberately preserves the known diagnostic-label mismatch that
  produced 77 passing and 7 failing tests. v2 restores the intended `[tool]`
  and `[completion]` labels and the 84-test green baseline.

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

Reconcile the accepted direction in
`docs/discussions/unified-execution-dispatch.md` and
`docs/discussions/control-plane-and-execution-plane.md` into an explicit
architecture amendment before starting milestone 03. Preserve the fixed
model-visible `exec`, `output`, and `kill` surface while deciding the immutable
Execution Plan, Host-owned execution policy, Shell-only first driver, and
control-plane/execution-plane boundary.
