"""Files and cd Handler backend-route contract and static dependency tests.

RFC-0012 issue 04 requires Files and ``cd`` to query the live Workspace only
through the Workspace Filesystem, with the exact-edit, BOM/CRLF, atomicity,
and capability semantics preserved inside the Backend.
"""

import asyncio
import importlib
import os
import posixpath
from pathlib import Path
from typing import Literal, cast

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._backend import (
    _BackendWorkspace,
    _FileEdit,
    _FileEditRequest,
    _FileEditResult,
    _FileMetadata,
    _FilesystemError,
    _FileWriteRequest,
    _FileWriteResult,
    _ResolvedPath,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.handlers.cd import _prepare_cd
from cli_agent.runtime._environment.sources import _FileSource
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExitStatus,
)


class _RecordingFilesystem:
    """Record every filesystem request without touching the disk."""

    def __init__(self, root: str | None = None) -> None:
        self.requests: list[object] = []
        self.resolve_requests: list[tuple[str, str]] = []
        self.stat_paths: list[str] = []
        self._root = root
        self._metadata: _FileMetadata | None = None
        self._stat_error: _FilesystemError | None = None

    def resolve(self, path: str, cwd: str) -> _ResolvedPath:
        self.resolve_requests.append((path, cwd))
        target = posixpath.normpath(
            path if posixpath.isabs(path) else posixpath.join(cwd, path)
        )
        root = self._root
        return _ResolvedPath(
            path=target,
            within_workspace=root is None
            or target == root
            or target.startswith(root.rstrip("/") + "/"),
        )

    def set_stat(self, metadata: _FileMetadata) -> None:
        self._metadata = metadata

    def set_stat_error(self, error: _FilesystemError) -> None:
        self._stat_error = error

    async def stat(self, path: str) -> _FileMetadata:
        self.stat_paths.append(path)
        if self._stat_error is not None:
            raise self._stat_error
        metadata = self._metadata
        if metadata is None:
            raise _FilesystemError("not_found", f"No such file or directory: {path}")
        return metadata

    async def write(self, request: _FileWriteRequest) -> _FileWriteResult:
        self.requests.append(request)
        return _FileWriteResult(path=request.path, bytes_written=len(request.content))

    async def edit(self, request: _FileEditRequest) -> _FileEditResult:
        self.requests.append(request)
        return _FileEditResult(path=request.path, blocks_replaced=len(request.edits))


class _BackendWithDistinctRoot:
    def __init__(self, filesystem: _RecordingFilesystem) -> None:
        self.root = "/workspace"
        self.filesystem = filesystem


class _RecordedOutput:
    def __init__(self) -> None:
        self._chunks: list[tuple[str, bytes]] = []

    async def write(
        self,
        stream: Literal["stdout", "stderr"],
        data: bytes,
    ) -> None:
        self._chunks.append((stream, data))

    def text(self, stream: str) -> str:
        return "".join(
            data.decode("utf-8") for name, data in self._chunks if name == stream
        )


def test_files_write_builds_one_resolved_request(tmp_path: Path) -> None:
    filesystem = _RecordingFilesystem()
    handler = _FileSource(filesystem)
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        environment={},
    )

    execution = handler.prepare(
        _ExecutionRequest(
            command=parse_shell_ast("files write notes/a.txt"),
            stdin="line\n",
        ),
        context,
    )
    outcome = asyncio.run(execution.run(_RecordedOutput()))

    assert outcome == ExitStatus(0)
    assert filesystem.requests == [
        _FileWriteRequest(
            path=str(tmp_path / "notes" / "a.txt"),
            content=b"line\n",
        )
    ]


def test_files_edit_builds_one_resolved_request(tmp_path: Path) -> None:
    filesystem = _RecordingFilesystem()
    handler = _FileSource(filesystem)
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        environment={},
    )

    execution = handler.prepare(
        _ExecutionRequest(
            command=parse_shell_ast("files edit notes/a.txt"),
            stdin='{"edits": [{"oldText": "a", "newText": "b"}]}',
        ),
        context,
    )
    outcome = asyncio.run(execution.run(_RecordedOutput()))

    assert outcome == ExitStatus(0)
    assert filesystem.requests == [
        _FileEditRequest(
            path=str(tmp_path / "notes" / "a.txt"),
            edits=(_FileEdit(old_text="a", new_text="b"),),
        )
    ]


def test_files_request_resolves_relative_to_backend_cwd(tmp_path: Path) -> None:
    filesystem = _RecordingFilesystem()
    subdirectory = tmp_path / "sub"
    handler = _FileSource(filesystem)
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(subdirectory),
        environment={},
    )

    execution = handler.prepare(
        _ExecutionRequest(
            command=parse_shell_ast("files write item.txt"),
            stdin="x\n",
        ),
        context,
    )
    asyncio.run(execution.run(_RecordedOutput()))

    request = filesystem.requests[0]
    assert isinstance(request, _FileWriteRequest)
    assert request.path == str(subdirectory / "item.txt")


def test_cd_stats_resolved_target_and_commits_backend_cwd(tmp_path: Path) -> None:
    filesystem = _RecordingFilesystem()
    filesystem.set_stat(_FileMetadata(kind="directory", size=0, mtime_ns=0, mode=0o755))
    committed: list[str] = []
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        environment={},
        set_cwd=committed.append,
    )
    execution = _prepare_cd(filesystem)(
        _ExecutionRequest(command=parse_shell_ast("cd sub")),
        context,
    )

    output = _RecordedOutput()
    outcome = asyncio.run(execution.run(output))

    assert outcome == ExitStatus(0)
    assert filesystem.stat_paths == [str(tmp_path / "sub")]
    assert committed == [str(tmp_path / "sub")]
    assert output.text("stdout") == str(tmp_path / "sub")


def test_cd_reports_missing_directory_from_filesystem_facts(tmp_path: Path) -> None:
    filesystem = _RecordingFilesystem()
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        environment={},
    )
    execution = _prepare_cd(filesystem)(
        _ExecutionRequest(command=parse_shell_ast("cd missing")),
        context,
    )

    output = _RecordedOutput()
    outcome = asyncio.run(execution.run(output))

    assert outcome == 1
    assert output.text("stderr") == (f"Directory not found: {tmp_path / 'missing'}\n")


def test_cd_rejects_non_directory_target(tmp_path: Path) -> None:
    filesystem = _RecordingFilesystem()
    filesystem.set_stat(_FileMetadata(kind="file", size=1, mtime_ns=0, mode=0o644))
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        environment={},
    )
    execution = _prepare_cd(filesystem)(
        _ExecutionRequest(command=parse_shell_ast("cd plain.txt")),
        context,
    )

    output = _RecordedOutput()
    outcome = asyncio.run(execution.run(output))

    assert outcome == 1
    assert output.text("stderr") == (f"Not a directory: {tmp_path / 'plain.txt'}\n")


def test_cd_preserves_non_directory_filesystem_errors() -> None:
    filesystem = _RecordingFilesystem(root="/workspace")
    filesystem.set_stat_error(
        _FilesystemError("permission_denied", "permission denied: /workspace/locked")
    )
    context = _CommandContext(
        workspace="/workspace",
        cwd="/workspace",
        environment={},
    )
    execution = _prepare_cd(filesystem)(
        _ExecutionRequest(command=parse_shell_ast("cd locked")),
        context,
    )

    output = _RecordedOutput()
    outcome = asyncio.run(execution.run(output))

    assert outcome == 1
    assert output.text("stderr") == (
        "failed to change directory to locked: permission denied: /workspace/locked\n"
    )


def test_kernel_uses_backend_root_for_files_and_default_cd(tmp_path: Path) -> None:
    async def scenario() -> None:
        filesystem = _RecordingFilesystem(root="/workspace")
        filesystem.set_stat(
            _FileMetadata(kind="directory", size=0, mtime_ns=0, mode=0o755)
        )
        backend = cast(
            _BackendWorkspace,
            _BackendWithDistinctRoot(filesystem),
        )
        kernel = EnvironmentKernel(tmp_path, backend=backend)
        try:
            written = await _exec(
                kernel,
                "files write note.txt",
                stdin="content\n",
            )
            changed = await _exec(kernel, "cd")
        finally:
            await kernel.close()

        assert _output(written)["status"] == "exited"
        assert _output(changed)["status"] == "exited"
        assert filesystem.resolve_requests == [
            ("note.txt", "/workspace"),
            ("/workspace", "/workspace"),
        ]
        assert filesystem.requests == [
            _FileWriteRequest(path="/workspace/note.txt", content=b"content\n")
        ]
        assert filesystem.stat_paths == ["/workspace"]

    asyncio.run(scenario())


def test_filesystem_execution_cancel_before_run_has_no_side_effects(
    tmp_path: Path,
) -> None:
    filesystem = _RecordingFilesystem()
    handler = _FileSource(filesystem)
    context = _CommandContext(
        workspace=str(tmp_path),
        cwd=str(tmp_path),
        environment={},
    )
    execution = handler.prepare(
        _ExecutionRequest(
            command=parse_shell_ast("files write a.txt"),
            stdin="x\n",
        ),
        context,
    )

    async def scenario() -> None:
        await execution.kill()
        outcome = await execution.run(_RecordedOutput())
        assert outcome == ExitStatus(_KILLED_BEFORE_START)

    asyncio.run(scenario())
    assert filesystem.requests == []


def test_cd_and_files_see_shell_created_paths(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            created = await _exec(
                kernel, "mkdir -p generated && echo data > generated/item.txt"
            )
            changed = await _exec(kernel, "cd generated")
            pwd = await _exec(kernel, "pwd")
            written = await _exec(
                kernel,
                "files write item.txt",
                stdin="replaced\n",
            )
            cat = await _exec(kernel, "cat item.txt")
        finally:
            await kernel.close()

        assert _output(created)["status"] == "exited"
        assert _output(changed)["status"] == "exited"
        assert _stream_text(_output(changed), "stdout") == str(tmp_path / "generated")
        assert _stream_text(_output(pwd), "stdout").strip() == str(
            tmp_path / "generated"
        )
        assert _output(written)["status"] == "exited"
        assert _stream_text(_output(cat), "stdout") == "replaced\n"

    asyncio.run(scenario())


def test_files_handler_module_has_no_host_io_mechanics() -> None:
    source = _module_source("cli_agent.runtime._environment.handlers.files")

    assert "pathlib" not in source
    assert "import os" not in source
    assert "mkstemp" not in source
    assert "read_bytes" not in source
    assert "os.replace" not in source
    assert "capability.view" not in source


def test_cd_handler_module_queries_only_through_filesystem() -> None:
    source = _module_source("cli_agent.runtime._environment.handlers.cd")

    assert "pathlib" not in source
    assert "exists" not in source
    assert "is_dir" not in source
    assert "read_bytes" not in source


async def _exec(
    kernel: EnvironmentKernel,
    command: str,
    *,
    stdin: str | None = None,
    wait_ms: int = 8_000,
) -> ToolResult:
    arguments: dict[str, object] = {"command": command, "wait_ms": wait_ms}
    if stdin is not None:
        arguments["stdin"] = stdin
    return await kernel.dispatch(
        ToolCall(
            call_id=f"exec_{id(command)}",
            name="exec",
            arguments=arguments,
        )
    )


def _output(result: ToolResult) -> dict[str, object]:
    assert result.error is None
    assert isinstance(result.output, dict)
    return result.output


def _stream_text(snapshot: dict[str, object], stream: str) -> str:
    chunks = snapshot["chunks"]
    assert isinstance(chunks, list)
    return "".join(
        chunk["text"]
        for chunk in chunks
        if isinstance(chunk, dict)
        and chunk.get("stream") == stream
        and isinstance(chunk.get("text"), str)
    )


def _module_source(module_name: str) -> str:
    module = importlib.import_module(module_name)
    return Path(os.path.abspath(module.__file__)).read_text(encoding="utf-8")
