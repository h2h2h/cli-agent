"""Workspace identity, Backend binding, filesystem contract, and mismatch."""

import asyncio
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

from cli_agent.errors.workspace import WorkspaceMismatchError
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ScriptedModelProvider,
    UserMessage,
)
from cli_agent.runtime._backend import (
    _BackendWorkspace,
    _FileEdit,
    _FileEditRequest,
    _FilesystemError,
    _FileWriteRequest,
    _WorkspaceFilesystem,
)
from cli_agent.runtime._workspace import (
    _load_workspace_identity,
    _LocalWorkspaceFactory,
)

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=128_000,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def _state(tmp_path: Path) -> Path:
    state = tmp_path / ".workspace"
    state.mkdir()
    return state


def test_identity_generates_once_and_reads_stably(tmp_path: Path) -> None:
    state = _state(tmp_path)

    first = _load_workspace_identity(state)
    second = _load_workspace_identity(state)

    assert first == second
    assert first.startswith("local:")
    assert len(first) == len("local:") + 32
    other = tmp_path / "other"
    other.mkdir()
    assert _load_workspace_identity(other) != first


def test_identity_fails_closed_on_corrupt_content(tmp_path: Path) -> None:
    state = _state(tmp_path)
    (state / "identity").write_text("not-an-identity\n", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid workspace identity"):
        _load_workspace_identity(state)


def test_factory_binds_stable_identity_and_backend(tmp_path: Path) -> None:
    other_root = tmp_path / "other"
    other_root.mkdir()

    async def scenario() -> None:
        first = await _LocalWorkspaceFactory().open(tmp_path, repertoire=None)
        second = await _LocalWorkspaceFactory().open(tmp_path, repertoire=None)
        other = await _LocalWorkspaceFactory().open(
            other_root,
            repertoire=None,
        )

        assert first.id == second.id
        assert first.id.startswith("local:")
        assert first.id != other.id
        assert first.root == str(tmp_path.resolve())
        assert isinstance(first.backend, _BackendWorkspace)
        assert isinstance(first.filesystem, _WorkspaceFilesystem)
        await first.close()
        await second.close()
        await other.close()

    asyncio.run(scenario())


def test_resolve_uses_backend_native_semantics(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = await _LocalWorkspaceFactory().open(tmp_path, repertoire=None)
        filesystem = workspace.filesystem
        root = str(tmp_path.resolve())

        inside = filesystem.resolve("notes.txt", cwd=f"{root}/sub")
        assert inside.path == str(tmp_path.resolve() / "sub" / "notes.txt")
        assert inside.within_workspace is True

        escaped = filesystem.resolve("../../outside.txt", cwd=f"{root}/sub")
        assert escaped.within_workspace is False

        absolute = filesystem.resolve(str(tmp_path.parent / "outside.txt"), cwd=root)
        assert absolute.path == str(tmp_path.parent / "outside.txt")
        assert absolute.within_workspace is False

        await workspace.close()

    asyncio.run(scenario())


def test_edit_is_atomic_and_never_partially_applies(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = await _LocalWorkspaceFactory().open(tmp_path, repertoire=None)
        filesystem = workspace.filesystem
        await filesystem.write(
            _FileWriteRequest(path="notes.txt", content=b"hello world")
        )

        with pytest.raises(_FilesystemError) as raised:
            await filesystem.edit(
                _FileEditRequest(
                    path="notes.txt",
                    edits=(
                        _FileEdit(old_text="hello", new_text="goodbye"),
                        _FileEdit(old_text="missing", new_text="replacement"),
                    ),
                )
            )

        assert raised.value.kind == "edit_failed"
        assert await filesystem.read("notes.txt") == b"hello world"
        await workspace.close()

    asyncio.run(scenario())


def test_close_forbids_filesystem_use(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = await _LocalWorkspaceFactory().open(tmp_path, repertoire=None)
        filesystem = workspace.filesystem
        await workspace.close()

        with pytest.raises(RuntimeError, match="Backend Workspace is closed"):
            filesystem.resolve("notes.txt", workspace.root)

    asyncio.run(scenario())


def _completion(message: AssistantMessage) -> ModelCompletion:
    return ModelCompletion(message=message, finish_reason="stop")


async def _collect_turn(
    runtime: AgentRuntime,
    message: str,
) -> tuple[object, ...]:
    return tuple(
        [
            event
            async for event in runtime.run_turn(UserMessage.text(message))
        ]
    )


def test_workspace_mismatch_fails_closed_before_provider_or_tools(
    tmp_path: Path,
) -> None:
    workspace_a = tmp_path / "project-a"
    workspace_a.mkdir()
    workspace_b = tmp_path / "project-b"
    workspace_b.mkdir()
    provider = ScriptedModelProvider(
        script=((_completion(AssistantMessage.text("done")),),)
    )

    async def scenario() -> None:
        runtime_a = await AgentRuntime.open(
            workspace=workspace_a,
            provider=provider,
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        session = await runtime_a.new_session()
        await _collect_turn(runtime_a, "hello")
        await runtime_a.close()

        runtime_b = await AgentRuntime.open(
            workspace=workspace_b,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        try:
            with pytest.raises(WorkspaceMismatchError) as raised:
                await runtime_b.resume_session(session.session_id)
        finally:
            await runtime_b.close()

        assert raised.value.code == "workspace_mismatch"
        assert raised.value.details == {
            "session_id": session.session_id,
            "workspace_id": runtime_a._resources.workspace.id,
            "expected_workspace_id": runtime_b._resources.workspace.id,
        }
        assert (
            raised.value.details["workspace_id"]
            != raised.value.details["expected_workspace_id"]
        )

    asyncio.run(scenario())
    assert len(provider.requests) == 1


def test_same_workspace_existing_session_resumes_explicitly(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        session = await runtime.new_session()
        await runtime.close()

        resumed = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            user_interaction=_user_interaction,
            context_policy=_context_policy,
        )
        try:
            fresh = await resumed.new_session()
            assert fresh.session_id != session.session_id
            restored = await resumed.resume_session(session.session_id)
            assert restored.session_id == session.session_id
        finally:
            await resumed.close()

    asyncio.run(scenario())
