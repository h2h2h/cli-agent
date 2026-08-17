from pathlib import Path

import pytest

import cli_agent._adapters.local.tool_runtime as tool_runtime_module
from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._environment import EnvironmentKernel


@pytest.fixture(autouse=True)
def _kernel_single_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """Expose a test-only single-call convenience on the real Kernel.

    Production code dispatches batches through ``EnvironmentKernel.dispatch``.
    The single-call form is installed here as a test convenience so existing
    test call sites that spell ``await kernel.dispatch(call)`` remain concise.
    """

    batch_dispatch = EnvironmentKernel.dispatch

    async def dispatch(
        self: EnvironmentKernel,
        calls: tuple[ToolCall, ...] | ToolCall,
    ) -> tuple[ToolResult, ...] | ToolResult:
        if isinstance(calls, ToolCall):
            return (await batch_dispatch(self, (calls,)))[0]
        return await batch_dispatch(self, calls)

    monkeypatch.setattr(EnvironmentKernel, "dispatch", dispatch)


@pytest.fixture(autouse=True)
def _isolate_default_repertoire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep the default per-user Repertoire inside pytest's temporary root."""

    home = tmp_path.parent / f"{tmp_path.name}-home"
    monkeypatch.setenv("HOME", str(home))


@pytest.fixture(autouse=True)
def _fast_tool_environment_sync(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Avoid real dependency resolution in ordinary tests.

    Each test normally gets a fresh temporary Workspace, so a real
    ``uv pip compile``/``sync`` would repeatedly resolve and install the
    Runtime-owned Tool dependencies. The explicit ``live_sync`` test remains
    available for validating the real worker environment.
    """

    if request.node.get_closest_marker("live_sync") is not None:
        return

    async def _noop_sync(
        *,
        python: Path,
        requirements: Path,
        working_directory: Path,
    ) -> None:
        del python, requirements, working_directory

    monkeypatch.setattr(
        tool_runtime_module,
        "_sync_requirements",
        _noop_sync,
    )
