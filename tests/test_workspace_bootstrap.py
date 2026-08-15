import asyncio
import os
import stat
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

from cli_agent.runtime import (
    AgentRuntime,
    ContextPolicy,
    ScriptedModelProvider,
)

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def test_runtime_open_bootstraps_and_preserves_workspace_state(
    tmp_path: Path,
) -> None:
    workspace_state = tmp_path / ".workspace"
    workspace_environment = workspace_state / "env"

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )

        assert workspace_state.is_dir()
        assert not workspace_state.is_symlink()
        assert workspace_environment.is_file()
        assert not workspace_environment.is_symlink()
        assert workspace_environment.read_bytes() == b""
        default_repertoire = Path.home() / ".cli-agent" / "repertoire"
        assert {path.name for path in default_repertoire.iterdir()} == {
            "library",
            "skills",
            "tools",
            "_mcp",
        }
        if os.name == "posix":
            assert stat.S_IMODE(workspace_state.stat().st_mode) & 0o077 == 0
            assert stat.S_IMODE(workspace_environment.stat().st_mode) & 0o077 == 0

        workspace_environment.write_text("PRESERVED=value\n", encoding="utf-8")
        await runtime.close()

        assert workspace_environment.read_text(encoding="utf-8") == (
            "PRESERVED=value\n"
        )
        assert workspace_state.is_dir()
        assert workspace_environment.is_file()

    asyncio.run(scenario())


def test_runtime_open_reuses_existing_workspace_state_without_changes(
    tmp_path: Path,
) -> None:
    workspace_state = tmp_path / ".workspace"
    workspace_state.mkdir()
    workspace_environment = workspace_state / "env"
    workspace_environment.write_text("EXISTING=preserved\n", encoding="utf-8")
    if os.name == "posix":
        workspace_state.chmod(0o755)
        workspace_environment.chmod(0o640)

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )
        await runtime.close()

    asyncio.run(scenario())

    assert workspace_environment.read_text(encoding="utf-8") == ("EXISTING=preserved\n")
    if os.name == "posix":
        assert stat.S_IMODE(workspace_state.stat().st_mode) == 0o755
        assert stat.S_IMODE(workspace_environment.stat().st_mode) == 0o640


def test_runtime_open_uses_explicit_repertoire(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repertoire = tmp_path / "repertoire"
    workspace.mkdir()
    lower = repertoire / "tools" / "selected.py"
    lower.parent.mkdir(parents=True)
    (repertoire / "skills").mkdir()
    (repertoire / "library").mkdir()
    lower.write_text("SELECTED = True\n", encoding="utf-8")

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=workspace,
            repertoire=repertoire,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )
        try:
            assert runtime._resources.capability_view._repertoire == repertoire
        finally:
            await runtime.close()

    asyncio.run(scenario())

    visible = workspace / ".workspace" / "tools" / "selected.py"
    assert visible.is_symlink()
    assert visible.read_text(encoding="utf-8") == "SELECTED = True\n"


def test_runtime_open_rejects_workspace_state_file_conflict(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".workspace"
    path.write_text("conflict", encoding="utf-8")

    async def scenario() -> None:
        with pytest.raises(
            ValueError,
            match="must be a real directory",
        ):
            await AgentRuntime.open(
                user_interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
                context_policy=_context_policy,
            )

    asyncio.run(scenario())


def test_runtime_open_rejects_workspace_environment_directory_conflict(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".workspace" / "env"
    path.mkdir(parents=True)

    async def scenario() -> None:
        with pytest.raises(
            ValueError,
            match="must be a real regular file",
        ):
            await AgentRuntime.open(
                user_interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
                context_policy=_context_policy,
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("conflict", "target_kind", "message"),
    (
        (".workspace", "directory", "must be a real directory"),
        (".workspace/env", "file", "must be a real regular file"),
    ),
)
def test_runtime_open_rejects_workspace_state_symbolic_links(
    tmp_path: Path,
    conflict: str,
    target_kind: str,
    message: str,
) -> None:
    path = tmp_path / conflict
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / f"{path.name}-target"
    if target_kind == "directory":
        target.mkdir()
    else:
        target.write_text("secret", encoding="utf-8")
    try:
        path.symlink_to(target, target_is_directory=target_kind == "directory")
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    async def scenario() -> None:
        with pytest.raises(ValueError, match=message):
            await AgentRuntime.open(
                user_interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
                context_policy=_context_policy,
            )

    asyncio.run(scenario())


def test_concurrent_runtime_opens_share_idempotent_bootstrap(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtimes = await asyncio.gather(
            *(
                AgentRuntime.open(
                    user_interaction=_user_interaction,
                    workspace=tmp_path,
                    provider=ScriptedModelProvider(script=()),
                    context_policy=_context_policy,
                )
                for _ in range(4)
            )
        )
        await asyncio.gather(*(runtime.close() for runtime in runtimes))

    asyncio.run(scenario())

    workspace_state = tmp_path / ".workspace"
    environment = workspace_state / "env"
    assert workspace_state.is_dir()
    assert environment.is_file()
    assert environment.read_bytes() == b""
    assert {path.name for path in workspace_state.iterdir()} == {
        ".capability-view",
        ".tool-environment",
        "_mcp",
        "env",
        "identity",
        "library",
        "skills",
        "tools",
    }


def test_runtime_open_requires_existing_workspace(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    async def scenario() -> None:
        with pytest.raises(
            ValueError,
            match="workspace must be an existing directory",
        ):
            await AgentRuntime.open(
                user_interaction=_user_interaction,
                workspace=missing,
                provider=ScriptedModelProvider(script=()),
                context_policy=_context_policy,
            )

    asyncio.run(scenario())

    assert not missing.exists()
