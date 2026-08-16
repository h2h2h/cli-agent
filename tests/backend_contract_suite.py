"""Shared Backend / filesystem contract suite for Local and Docker.

Issue 016 parametrizes the Backend Workspace contract over the Local and
Docker implementations: prepare is synchronous and free of side effects,
``run`` pushes stdout/stderr and returns the normalized exit status,
stdin and the three-phase kill contract hold, and the filesystem keeps
Backend-native ``resolve`` plus atomic ``write`` / ``edit`` semantics.

Each suite case receives an async ``open_workspace`` callable returning a
fresh ``Backend`` and is executed inside ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

import pytest

from cli_agent.runtime._backend.facts import (
    _FileEdit,
    _FileEditRequest,
    _FileEditResult,
    _FilesystemError,
    _FileWriteRequest,
    _FileWriteResult,
    _ResolvedPath,
    _ShellExecutionRequest,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExecutionOutputSink,
    ExitStatus,
)

_OpenWorkspace = Callable[[], Awaitable[object]]


class _BufferOutput(ExecutionOutputSink):
    """Collect one execution's stdout and stderr frames in memory."""

    def __init__(self) -> None:
        self.chunks: list[tuple[str, bytes]] = []

    async def write(self, stream: Literal["stdout", "stderr"], data: bytes) -> None:
        self.chunks.append((stream, data))

    def text(self, stream: str) -> str:
        return "".join(
            data.decode("utf-8") for name, data in self.chunks if name == stream
        )


def _request(
    command: str,
    *,
    cwd: str,
    environment: dict[str, str] | None = None,
    input_data: bytes | None = None,
) -> _ShellExecutionRequest:
    return _ShellExecutionRequest(
        command=parse_shell_ast(command),
        cwd=cwd,
        environment=environment or {},
        input_data=input_data,
    )


async def _assert_prepare_is_synchronous_and_defer_resources(
    open_workspace: _OpenWorkspace,
) -> None:
    workspace = await open_workspace()
    try:
        execution = workspace.prepare_shell(  # type: ignore[attr-defined]
            _request("echo prepared", cwd=workspace.root)
        )
        assert not asyncio.iscoroutine(execution)
    finally:
        await workspace.close()  # type: ignore[attr-defined]


async def _assert_run_streams_and_exit_status(
    open_workspace: _OpenWorkspace,
) -> None:
    workspace = await open_workspace()
    try:
        output = _BufferOutput()
        outcome = await workspace.prepare_shell(  # type: ignore[attr-defined]
            _request("echo out; echo err >&2; exit 3", cwd=workspace.root)
        ).run(output)
        assert outcome == ExitStatus(3)
        assert output.text("stdout") == "out\n"
        assert output.text("stderr") == "err\n"
    finally:
        await workspace.close()  # type: ignore[attr-defined]


async def _assert_stdin_round_trip(open_workspace: _OpenWorkspace) -> None:
    workspace = await open_workspace()
    try:
        output = _BufferOutput()
        outcome = await workspace.prepare_shell(  # type: ignore[attr-defined]
            _request(
                "cat",
                cwd=workspace.root,
                input_data=b"streamed payload\n",
            )
        ).run(output)
        assert outcome == ExitStatus(0)
        assert output.text("stdout") == "streamed payload\n"
    finally:
        await workspace.close()  # type: ignore[attr-defined]


async def _assert_kill_before_run(open_workspace: _OpenWorkspace) -> None:
    workspace = await open_workspace()
    try:
        execution = workspace.prepare_shell(  # type: ignore[attr-defined]
            _request("sleep 30", cwd=workspace.root)
        )
        await execution.kill()
        output = _BufferOutput()
        assert await execution.run(output) == ExitStatus(_KILLED_BEFORE_START)
        assert output.chunks == []
    finally:
        await workspace.close()  # type: ignore[attr-defined]


async def _assert_kill_during_run(open_workspace: _OpenWorkspace) -> None:
    workspace = await open_workspace()
    try:
        execution = workspace.prepare_shell(  # type: ignore[attr-defined]
            _request("sleep 30", cwd=workspace.root)
        )
        task = asyncio.create_task(execution.run(_BufferOutput()))
        await asyncio.sleep(0.3)
        await execution.kill()
        outcome = await asyncio.wait_for(task, timeout=10)
        assert outcome > 128, "killed execution must report a signal-normalized code"
    finally:
        await workspace.close()  # type: ignore[attr-defined]


async def _assert_kill_after_terminal(open_workspace: _OpenWorkspace) -> None:
    workspace = await open_workspace()
    try:
        execution = workspace.prepare_shell(  # type: ignore[attr-defined]
            _request("echo done", cwd=workspace.root)
        )
        output = _BufferOutput()
        assert await execution.run(output) == ExitStatus(0)
        await execution.kill()
        assert output.text("stdout") == "done\n"
    finally:
        await workspace.close()  # type: ignore[attr-defined]


async def _assert_filesystem_round_trip(open_workspace: _OpenWorkspace) -> None:
    workspace = await open_workspace()
    try:
        filesystem = workspace.filesystem  # type: ignore[attr-defined]
        result = await filesystem.write(
            _FileWriteRequest(path="notes/a.txt", content=b"hello world")
        )
        assert result == _FileWriteResult(path="notes/a.txt", bytes_written=11)
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
        with pytest.raises(_FilesystemError) as missing:
            await filesystem.stat("notes/a.txt")
        assert missing.value.kind == "not_found"
    finally:
        await workspace.close()  # type: ignore[attr-defined]


async def _assert_resolve_uses_backend_native_semantics(
    open_workspace: _OpenWorkspace,
) -> None:
    workspace = await open_workspace()
    try:
        filesystem = workspace.filesystem  # type: ignore[attr-defined]
        root = workspace.root
        resolved = filesystem.resolve("sub/../a.txt", root)
        assert isinstance(resolved, _ResolvedPath)
        assert resolved.within_workspace
        assert resolved.path.endswith("a.txt")
        inside = filesystem.resolve("sub/a.txt", root)
        assert inside.within_workspace
        outside = filesystem.resolve("/etc/passwd", root)
        assert not outside.within_workspace
    finally:
        await workspace.close()  # type: ignore[attr-defined]


async def _assert_edit_is_atomic_and_shared(open_workspace: _OpenWorkspace) -> None:
    workspace = await open_workspace()
    try:
        filesystem = workspace.filesystem  # type: ignore[attr-defined]
        await filesystem.write(
            _FileWriteRequest(path="script.sh", content=b"#!/bin/sh\nVALUE=1\n")
        )
        result = await filesystem.edit(
            _FileEditRequest(
                path="script.sh",
                edits=(_FileEdit(old_text="VALUE=1", new_text="VALUE=2"),),
            )
        )
        assert result == _FileEditResult(path="script.sh", blocks_replaced=1)
        assert await filesystem.read("script.sh") == b"#!/bin/sh\nVALUE=2\n"
    finally:
        await workspace.close()  # type: ignore[attr-defined]


async def _assert_filesystem_error_kinds(open_workspace: _OpenWorkspace) -> None:
    workspace = await open_workspace()
    try:
        filesystem = workspace.filesystem  # type: ignore[attr-defined]
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
        with pytest.raises(_FilesystemError) as missing_remove:
            await filesystem.remove("missing.txt")
        assert missing_remove.value.kind == "not_found"
    finally:
        await workspace.close()  # type: ignore[attr-defined]


async def _assert_flush_and_idempotent_close(open_workspace: _OpenWorkspace) -> None:
    workspace = await open_workspace()
    await workspace.flush()  # type: ignore[attr-defined]
    await workspace.close()  # type: ignore[attr-defined]
    await workspace.close()  # type: ignore[attr-defined]


CONTRACT_CASES: tuple[Callable[[_OpenWorkspace], Awaitable[None]], ...] = (
    _assert_prepare_is_synchronous_and_defer_resources,
    _assert_run_streams_and_exit_status,
    _assert_stdin_round_trip,
    _assert_kill_before_run,
    _assert_kill_during_run,
    _assert_kill_after_terminal,
    _assert_filesystem_round_trip,
    _assert_resolve_uses_backend_native_semantics,
    _assert_edit_is_atomic_and_shared,
    _assert_filesystem_error_kinds,
    _assert_flush_and_idempotent_close,
)


async def run_contract_suite(open_workspace: _OpenWorkspace) -> None:
    """Execute every Backend Workspace contract assertion.

    Args:
        open_workspace: Async factory returning one fresh Backend Workspace
            that the assertions close when they finish.
    """

    for case in CONTRACT_CASES:
        await case(open_workspace)
