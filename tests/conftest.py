from pathlib import Path

import pytest

import cli_agent.runtime._backend.local.tool_runtime as tool_runtime_module


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
