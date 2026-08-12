"""Project instruction loader unit and Local Backend integration tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cli_agent.runtime._backend import (
    _CapabilitySource,
    _CapabilityState,
    _FileMetadata,
    _FilesystemError,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.facts import _FileKind
from cli_agent.runtime._backend.local import _LocalBackend
from cli_agent.runtime._backend.local.filesystem import _LocalWorkspaceFilesystem
from cli_agent.runtime._project_instructions import (
    MAX_PROJECT_INSTRUCTION_BYTES,
    _load_project_instructions,
    _ProjectInstructions,
)

_AGENTS = "AGENTS.md"


class _FakeFilesystem:
    """Scripted Workspace Filesystem for loader error and race branches."""

    def __init__(
        self,
        *,
        metadata: _FileMetadata | None = None,
        content: bytes | None = None,
        stat_error: _FilesystemError | None = None,
        read_error: _FilesystemError | None = None,
    ) -> None:
        self._metadata = metadata
        self._content = content
        self._stat_error = stat_error
        self._read_error = read_error
        self.stat_calls = 0
        self.read_calls = 0

    async def stat(self, path: str) -> _FileMetadata:
        self.stat_calls += 1
        if self._stat_error is not None:
            raise self._stat_error
        assert self._metadata is not None
        return self._metadata

    async def read(self, path: str) -> bytes:
        self.read_calls += 1
        if self._read_error is not None:
            raise self._read_error
        assert self._content is not None
        return self._content


def _metadata(kind: _FileKind = "file", size: int = 0) -> _FileMetadata:
    return _FileMetadata(kind=kind, size=size, mtime_ns=0, mode=0o644)


def _run(coro) -> object:
    return asyncio.run(coro)


def test_missing_file_returns_none() -> None:
    filesystem = _FakeFilesystem(
        stat_error=_FilesystemError("not_found", "No such file or directory: x")
    )

    result = _run(_load_project_instructions(filesystem, "/workspace"))

    assert result is None
    assert filesystem.read_calls == 0


def test_empty_file_returns_none() -> None:
    filesystem = _FakeFilesystem(metadata=_metadata(size=0), content=b"")

    assert _run(_load_project_instructions(filesystem, "/workspace")) is None


def test_whitespace_only_file_returns_none() -> None:
    for content in (b"   \n\t", "\u00a0\u2002\n".encode("utf-8")):
        filesystem = _FakeFilesystem(
            metadata=_metadata(size=len(content)),
            content=content,
        )

        assert _run(_load_project_instructions(filesystem, "/workspace")) is None


def test_regular_utf8_content_returns_snapshot(tmp_path: Path) -> None:
    source = str(tmp_path / _AGENTS)
    content = "# Project rules\n\nrun `uv run pytest`.\n".encode("utf-8")
    filesystem = _FakeFilesystem(
        metadata=_metadata(size=len(content)),
        content=content,
    )

    result = _run(_load_project_instructions(filesystem, str(tmp_path)))

    assert result == _ProjectInstructions(source=source, text=content.decode("utf-8"))


def test_crlf_and_multibyte_content_are_preserved(tmp_path: Path) -> None:
    content = "# 构建\r\n使用 `uv run pytest`。\n".encode("utf-8")
    filesystem = _FakeFilesystem(
        metadata=_metadata(size=len(content)),
        content=content,
    )

    result = _run(_load_project_instructions(filesystem, str(tmp_path)))

    assert result is not None
    assert result.text == "# 构建\r\n使用 `uv run pytest`。\n"


def test_directory_kind_fails(tmp_path: Path) -> None:
    filesystem = _FakeFilesystem(metadata=_metadata(kind="directory"))

    with pytest.raises(ValueError, match="expected a regular file, found directory"):
        _run(_load_project_instructions(filesystem, str(tmp_path)))


def test_other_kind_fails(tmp_path: Path) -> None:
    filesystem = _FakeFilesystem(metadata=_metadata(kind="other"))

    with pytest.raises(ValueError, match="expected a regular file, found other"):
        _run(_load_project_instructions(filesystem, str(tmp_path)))


def test_stat_over_limit_fails_before_read(tmp_path: Path) -> None:
    filesystem = _FakeFilesystem(
        metadata=_metadata(size=MAX_PROJECT_INSTRUCTION_BYTES + 1),
        content=b"x",
    )

    with pytest.raises(ValueError, match="32768-byte limit"):
        _run(_load_project_instructions(filesystem, str(tmp_path)))

    assert filesystem.read_calls == 0


def test_read_race_over_limit_fails_after_under_limit_stat(tmp_path: Path) -> None:
    filesystem = _FakeFilesystem(
        metadata=_metadata(size=0),
        content=b"x" * (MAX_PROJECT_INSTRUCTION_BYTES + 1),
    )

    with pytest.raises(ValueError, match="32768-byte limit"):
        _run(_load_project_instructions(filesystem, str(tmp_path)))


def test_maximum_size_succeeds(tmp_path: Path) -> None:
    content = b"x" * MAX_PROJECT_INSTRUCTION_BYTES
    filesystem = _FakeFilesystem(
        metadata=_metadata(size=len(content)),
        content=content,
    )

    result = _run(_load_project_instructions(filesystem, str(tmp_path)))

    assert result is not None
    assert result.text == content.decode("utf-8")


def test_invalid_utf8_fails_with_source(tmp_path: Path) -> None:
    content = b"# rules\n\xff\xfe broken"
    filesystem = _FakeFilesystem(
        metadata=_metadata(size=len(content)),
        content=content,
    )

    with pytest.raises(ValueError, match=str(tmp_path)) as excinfo:
        _run(_load_project_instructions(filesystem, str(tmp_path)))

    assert "decode" in str(excinfo.value)
    assert "not valid UTF-8" in str(excinfo.value)


def test_stat_backend_error_fails_with_source(tmp_path: Path) -> None:
    filesystem = _FakeFilesystem(
        stat_error=_FilesystemError("permission_denied", "permission denied: x")
    )

    with pytest.raises(ValueError, match=str(tmp_path)) as excinfo:
        _run(_load_project_instructions(filesystem, str(tmp_path)))

    assert "inspect" in str(excinfo.value)
    assert "permission denied" in str(excinfo.value)


def test_read_backend_error_fails_with_source(tmp_path: Path) -> None:
    filesystem = _FakeFilesystem(
        metadata=_metadata(size=4),
        read_error=_FilesystemError("internal", "filesystem error for x: boom"),
    )

    with pytest.raises(ValueError, match=str(tmp_path)) as excinfo:
        _run(_load_project_instructions(filesystem, str(tmp_path)))

    assert "read" in str(excinfo.value)
    assert "boom" in str(excinfo.value)


async def _open_workspace(root: Path) -> _LocalWorkspaceFilesystem:
    env = root / ".workspace" / "env"
    env.parent.mkdir()
    env.write_text("", encoding="utf-8")
    repertoire = root / "repertoire"
    repertoire.mkdir(exist_ok=True)
    workspace = await _LocalBackend().open_workspace(
        source=_WorkspaceSource(root=root, environment=env),
        capability_source=_CapabilitySource(repertoire=repertoire),
        capability_state=_CapabilityState(root=root / ".workspace"),
    )
    return workspace.filesystem


def test_local_backend_loads_real_file(tmp_path: Path) -> None:
    content = "# Project\n\n- build with `make`\n"
    (tmp_path / _AGENTS).write_text(content, encoding="utf-8")

    async def scenario() -> None:
        filesystem = await _open_workspace(tmp_path)

        result = await _load_project_instructions(filesystem, str(tmp_path.resolve()))

        assert result == _ProjectInstructions(
            source=str(tmp_path.resolve() / _AGENTS),
            text=content,
        )

    asyncio.run(scenario())


def test_local_backend_loads_symlink_to_regular_file(tmp_path: Path) -> None:
    target = tmp_path / "rules.md"
    target.write_text("# linked rules\n", encoding="utf-8")
    (tmp_path / _AGENTS).symlink_to(target.name)

    async def scenario() -> None:
        filesystem = await _open_workspace(tmp_path)

        result = await _load_project_instructions(filesystem, str(tmp_path.resolve()))

        assert result is not None
        assert result.text == "# linked rules\n"

    asyncio.run(scenario())


def test_local_backend_directory_fails(tmp_path: Path) -> None:
    (tmp_path / _AGENTS).mkdir()

    async def scenario() -> None:
        filesystem = await _open_workspace(tmp_path)

        with pytest.raises(ValueError, match="expected a regular file"):
            await _load_project_instructions(filesystem, str(tmp_path.resolve()))

    asyncio.run(scenario())


def test_local_backend_broken_symlink_returns_none(tmp_path: Path) -> None:
    (tmp_path / _AGENTS).symlink_to("missing-target.md")

    async def scenario() -> None:
        filesystem = await _open_workspace(tmp_path)

        result = await _load_project_instructions(filesystem, str(tmp_path.resolve()))

        assert result is None

    asyncio.run(scenario())


def test_local_backend_invalid_utf8_fails(tmp_path: Path) -> None:
    (tmp_path / _AGENTS).write_bytes(b"# rules\n\xff\xfe broken")

    async def scenario() -> None:
        filesystem = await _open_workspace(tmp_path)

        with pytest.raises(ValueError, match="not valid UTF-8"):
            await _load_project_instructions(filesystem, str(tmp_path.resolve()))

    asyncio.run(scenario())
