"""Local Backend Workspace open, environment, and basic filesystem tests."""

import asyncio
from pathlib import Path

import pytest

from cli_agent.runtime._backend import (
    Backend,
    _FileEdit,
    _FileEditRequest,
    _FileEditResult,
    _FilesystemError,
    _FileWriteRequest,
    _FileWriteResult,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.local import (
    _LocalBackend,
    _LocalBackendWorkspace,
    _LocalWorkspaceFilesystem,
)


async def _open_workspace(
    root: Path,
    *,
    environment: str = "",
) -> _LocalBackendWorkspace:
    env = root / ".workspace" / "env"
    env.parent.mkdir()
    env.write_text(environment, encoding="utf-8")
    return await _LocalBackend().open_workspace(
        source=_WorkspaceSource(root=root, environment=env),
    )


def test_open_exposes_resolved_host_root_as_backend_path(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = await _open_workspace(tmp_path)

        assert isinstance(workspace, Backend)
        assert isinstance(workspace.filesystem, _LocalWorkspaceFilesystem)
        assert workspace.root == str(tmp_path.resolve())

    asyncio.run(scenario())


def test_open_owns_workspace_environment_and_merges_host_ambient(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOST_KEY", "host-value")
    monkeypatch.setenv("SHARED_KEY", "from-host")

    async def scenario() -> None:
        workspace = await _open_workspace(
            tmp_path,
            environment="SHARED_KEY=from-workspace\nWORKSPACE_KEY=workspace-value\n",
        )

        assert dict(workspace.workspace_environment) == {
            "SHARED_KEY": "from-workspace",
            "WORKSPACE_KEY": "workspace-value",
        }
        base = workspace.execution_base_environment()
        assert base["HOST_KEY"] == "host-value"
        assert base["WORKSPACE_KEY"] == "workspace-value"
        assert base["SHARED_KEY"] == "from-workspace"

    asyncio.run(scenario())


def test_open_fails_closed_when_workspace_root_is_missing(tmp_path: Path) -> None:
    async def scenario() -> None:
        env = tmp_path / "missing" / ".workspace" / "env"
        with pytest.raises(ValueError, match="existing directory"):
            await _LocalBackend().open_workspace(
                source=_WorkspaceSource(root=tmp_path / "missing", environment=env),
            )

    asyncio.run(scenario())


def test_open_fails_closed_when_environment_is_invalid(tmp_path: Path) -> None:
    async def scenario() -> None:
        with pytest.raises(ValueError, match="must not contain NUL"):
            await _open_workspace(tmp_path, environment="A\x00B=1\n")

    asyncio.run(scenario())


def test_filesystem_write_read_stat_list_edit_remove_round_trip(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        filesystem = (await _open_workspace(tmp_path)).filesystem

        result = await filesystem.write(
            _FileWriteRequest(path="notes/a.txt", content=b"hello world")
        )
        assert result == _FileWriteResult(path="notes/a.txt", bytes_written=11)
        assert (tmp_path / "notes" / "a.txt").read_bytes() == b"hello world"

        assert await filesystem.read("notes/a.txt") == b"hello world"
        metadata = await filesystem.stat("notes/a.txt")
        assert (metadata.kind, metadata.size, metadata.mode) == ("file", 11, 0o644)

        entries = await filesystem.list("notes")
        assert tuple(entry.name for entry in entries) == ("a.txt",)
        assert entries[0].metadata.kind == "file"

        edited = await filesystem.edit(
            _FileEditRequest(
                path="notes/a.txt",
                edits=(_FileEdit(old_text="hello", new_text="goodbye"),),
            )
        )
        assert edited == _FileEditResult(path="notes/a.txt", blocks_replaced=1)
        assert await filesystem.read("notes/a.txt") == b"goodbye world"

        await filesystem.remove("notes/a.txt")
        assert not (tmp_path / "notes" / "a.txt").exists()

    asyncio.run(scenario())


def test_filesystem_write_preserves_existing_mode(tmp_path: Path) -> None:
    async def scenario() -> None:
        filesystem = (await _open_workspace(tmp_path)).filesystem
        target = tmp_path / "script.sh"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o755)

        await filesystem.write(
            _FileWriteRequest(path="script.sh", content=b"#!/bin/sh\nupdated\n")
        )

        assert (tmp_path / "script.sh").stat().st_mode & 0o777 == 0o755

    asyncio.run(scenario())


def test_filesystem_reports_neutral_error_kinds(tmp_path: Path) -> None:
    async def scenario() -> None:
        filesystem = (await _open_workspace(tmp_path)).filesystem
        (tmp_path / "sub").mkdir()

        with pytest.raises(_FilesystemError) as missing:
            await filesystem.stat("missing.txt")
        assert missing.value.kind == "not_found"

        await filesystem.write(_FileWriteRequest(path="a.txt", content=b"x"))
        with pytest.raises(_FilesystemError) as listed_file:
            await filesystem.list("a.txt")
        assert listed_file.value.kind == "not_a_directory"

        with pytest.raises(_FilesystemError) as nested_file:
            await filesystem.read("a.txt/child")
        assert nested_file.value.kind == "not_a_directory"

        with pytest.raises(_FilesystemError) as directory_read:
            await filesystem.read("sub")
        assert directory_read.value.kind == "is_directory"

        with pytest.raises(_FilesystemError) as directory_remove:
            await filesystem.remove("sub")
        assert directory_remove.value.kind == "is_directory"

        await filesystem.remove("sub", recursive=True)
        with pytest.raises(_FilesystemError) as missing_remove:
            await filesystem.remove("missing.txt")
        assert missing_remove.value.kind == "not_found"

    asyncio.run(scenario())


def test_stat_follows_symlinks_like_posix_stat(tmp_path: Path) -> None:
    async def scenario() -> None:
        filesystem = (await _open_workspace(tmp_path)).filesystem
        (tmp_path / "real").mkdir()
        (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)

        metadata = await filesystem.stat("link")

        assert metadata.kind == "directory"

    asyncio.run(scenario())


def test_flush_is_noop_and_close_is_idempotent(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = await _open_workspace(tmp_path)

        await workspace.flush()
        await workspace.close()
        await workspace.close()

    asyncio.run(scenario())


def test_close_forbids_workspace_and_borrowed_resource_use(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = await _open_workspace(tmp_path)
        (tmp_path / "visible.txt").write_text("content", encoding="utf-8")

        await workspace.close()

        with pytest.raises(RuntimeError, match="Backend Workspace is closed"):
            workspace.filesystem.resolve("visible.txt", workspace.root)
        with pytest.raises(RuntimeError, match="Backend Workspace is closed"):
            await workspace.filesystem.read("visible.txt")

    asyncio.run(scenario())
