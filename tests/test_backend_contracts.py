"""Backend Workspace contract and fact shape tests.

These tests pin the private Backend domain introduced by RFC-0012 issue 01:
the contracts cover execution and filesystem (not just subprocess), every
path and result fact is backend-neutral data, and the existing
``ExecutionHandle`` contract is reused without modification.
"""

import asyncio
import posixpath
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import get_args, get_origin, get_type_hints

from cli_agent.runtime._backend import (
    _Backend,
    _BackendWorkspace,
    _BoundCapabilityView,
    _CapabilityInspection,
    _CapabilitySource,
    _CapabilityState,
    _DirectoryEntry,
    _FileEdit,
    _FileEditRequest,
    _FileEditResult,
    _FileMetadata,
    _FilesystemError,
    _FileWriteRequest,
    _FileWriteResult,
    _MCPServerFacts,
    _MCPToolFacts,
    _ResolvedPath,
    _ShellExecutionRequest,
    _ToolBinding,
    _ToolExecutionRequest,
    _ToolRuntimeStatus,
    _WorkspaceFilesystem,
    _WorkspaceMCPRuntime,
    _WorkspaceSource,
)
from cli_agent.runtime._capability.command_parser import (
    ShellParseResult,
    parse_shell_ast,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._execution import (
    ExecutionHandle,
    ExecutionOutputSink,
    ExitStatus,
)

_BACKEND_NEUTRAL_FACTS = (
    _ShellExecutionRequest,
    _ToolExecutionRequest,
    _ToolBinding,
    _FileMetadata,
    _ResolvedPath,
    _DirectoryEntry,
    _FileWriteRequest,
    _FileWriteResult,
    _FileEdit,
    _FileEditRequest,
    _FileEditResult,
    _CapabilityInspection,
    _MCPServerFacts,
    _MCPToolFacts,
    _ToolRuntimeStatus,
)

_HOST_SOURCE_FACTS = (_WorkspaceSource, _CapabilitySource, _CapabilityState)


class _NullOutput:
    async def write(self, stream: str, data: bytes) -> None:
        del stream, data


class _FakeFilesystem:
    """Minimal in-memory Workspace Filesystem fake for contract tests."""

    def __init__(self) -> None:
        self._files: dict[str, bytes] = {}

    def resolve(self, path: str, cwd: str) -> _ResolvedPath:
        target = posixpath.normpath(
            path if posixpath.isabs(path) else posixpath.join(cwd, path)
        )
        return _ResolvedPath(
            path=target,
            within_workspace=target == "/workspace" or target.startswith("/workspace/"),
        )

    async def stat(self, path: str) -> _FileMetadata:
        try:
            content = self._files[path]
        except KeyError:
            raise _FilesystemError("not_found", f"no such file: {path}") from None
        return _FileMetadata(kind="file", size=len(content), mtime_ns=0, mode=0o644)

    async def list(self, path: str) -> tuple[_DirectoryEntry, ...]:
        prefix = path.rstrip("/") + "/"
        return tuple(
            _DirectoryEntry(name=name[len(prefix) :], metadata=await self.stat(name))
            for name in sorted(self._files)
            if name.startswith(prefix)
        )

    async def read(self, path: str) -> bytes:
        try:
            return self._files[path]
        except KeyError:
            raise _FilesystemError("not_found", f"no such file: {path}") from None

    async def write(self, request: _FileWriteRequest) -> _FileWriteResult:
        self._files[request.path] = request.content
        return _FileWriteResult(path=request.path, bytes_written=len(request.content))

    async def edit(self, request: _FileEditRequest) -> _FileEditResult:
        content = (await self.read(request.path)).decode("utf-8")
        for edit in request.edits:
            if edit.old_text not in content:
                raise _FilesystemError(
                    "not_found", f"edit target not found: {request.path}"
                )
            content = content.replace(edit.old_text, edit.new_text, 1)
        self._files[request.path] = content.encode("utf-8")
        return _FileEditResult(path=request.path, blocks_replaced=len(request.edits))

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        del path, recursive
        raise _FilesystemError("unsupported", "fake filesystem removes nothing")


class _FakeCapabilityView:
    """Empty in-memory Bound Capability View fake."""

    root = "/workspace"

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        return _CapabilityInspection(
            relative_path=relative_path,
            provenance=None,
            shadows_repertoire=False,
            valid=True,
            validation_error=None,
        )

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        del relative_path
        return ()

    async def read(self, relative_path: str) -> bytes:
        del relative_path
        return b""

    async def stat(self, relative_path: str) -> _FileMetadata:
        del relative_path
        return _FileMetadata(kind="directory", size=0, mtime_ns=0, mode=0o700)


class _InMemoryCapabilityView:
    """Bound Capability View fake with no symlink mechanics.

    Proves the Bound View contract is implementable without Host symlink,
    copy-up or whiteout machinery: effective files live in one logical
    dictionary, and provenance is derived from a plain membership rule.
    """

    root = "/workspace"
    _BACKING = {
        "tools/math.py": b"def add(a, b):\n    return a + b\n",
        "skills/review/SKILL.md": b"# Review\n",
    }

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        if relative_path not in self._BACKING:
            return _CapabilityInspection(
                relative_path=relative_path,
                provenance=None,
                shadows_repertoire=False,
                valid=True,
                validation_error=None,
            )
        return _CapabilityInspection(
            relative_path=relative_path,
            provenance="repertoire",
            shadows_repertoire=False,
            valid=True,
            validation_error=None,
        )

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        prefix = relative_path.rstrip("/") + "/"
        entries: list[_DirectoryEntry] = []
        for name in sorted(self._BACKING):
            if name.startswith(prefix) and "/" not in name[len(prefix) :]:
                entries.append(
                    _DirectoryEntry(
                        name=name[len(prefix) :],
                        metadata=await self.stat(name),
                    )
                )
        return tuple(entries)

    async def read(self, relative_path: str) -> bytes:
        return self._BACKING[relative_path]

    async def stat(self, relative_path: str) -> _FileMetadata:
        if relative_path not in self._BACKING:
            raise _FilesystemError("not_found", f"no such file: {relative_path}")
        content = self._BACKING[relative_path]
        return _FileMetadata(
            kind="file",
            size=len(content),
            mtime_ns=0,
            mode=0o644,
        )


class _FakeMCPRuntime:
    """Empty Workspace MCP Runtime fake."""

    async def discover(self, configs, on_diagnostic=None):
        del configs, on_diagnostic
        return ()

    async def materialize_binding(self, configs):
        del configs
        return None


class _FakeBackendWorkspace:
    """Contract-conforming Backend Workspace fake built on existing Executions."""

    def __init__(self) -> None:
        self.root = "/workspace"
        self.filesystem = _FakeFilesystem()
        self.capabilities = _FakeCapabilityView()
        self.mcp = _FakeMCPRuntime()

    def prepare_shell(
        self,
        request: _ShellExecutionRequest,
    ) -> ExecutionHandle:
        async def execute(output: ExecutionOutputSink) -> ExitStatus:
            await output.write("stdout", request.command.raw_command.encode())
            return ExitStatus(0)

        return _InlineExecution(execute)

    def prepare_tool(
        self,
        request: _ToolExecutionRequest,
    ) -> ExecutionHandle:
        async def execute(output: ExecutionOutputSink) -> ExitStatus:
            await output.write("stdout", request.code.encode())
            return ExitStatus(0)

        return _InlineExecution(execute)

    async def reconcile_tool_runtime(self) -> _ToolRuntimeStatus:
        return _ToolRuntimeStatus(available=True, error=None)

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        return None


class _FakeBackend:
    """Contract-conforming Backend fake that opens the fake Workspace."""

    async def open_workspace(
        self,
        source: _WorkspaceSource,
        capability_source: _CapabilitySource,
        capability_state: _CapabilityState,
    ) -> _BackendWorkspace:
        del source, capability_source, capability_state
        return _FakeBackendWorkspace()


def test_contracts_cover_execution_and_filesystem() -> None:
    backend = _FakeBackend()
    workspace = _FakeBackendWorkspace()

    assert isinstance(backend, _Backend)
    assert isinstance(workspace, _BackendWorkspace)
    assert isinstance(workspace.filesystem, _WorkspaceFilesystem)
    assert isinstance(workspace.capabilities, _BoundCapabilityView)
    assert isinstance(workspace.mcp, _WorkspaceMCPRuntime)


def test_facts_are_frozen_slots_backend_neutral_data() -> None:
    for fact in _BACKEND_NEUTRAL_FACTS:
        assert is_dataclass(fact)
        assert fact.__dataclass_params__.frozen
        assert fact.__dataclass_params__.slots
        for annotation in get_type_hints(fact).values():
            assert _is_backend_neutral_type(annotation), (fact.__name__, annotation)


def test_host_paths_are_confined_to_open_input_facts() -> None:
    for fact in _BACKEND_NEUTRAL_FACTS:
        assert not any(
            annotation is Path for annotation in get_type_hints(fact).values()
        )
    for fact in _HOST_SOURCE_FACTS:
        assert any(annotation is Path for annotation in get_type_hints(fact).values())


def test_shell_request_carries_parse_facts_and_opaque_paths() -> None:
    request = _ShellExecutionRequest(
        command=parse_shell_ast("echo hi"),
        cwd="/workspace",
        environment={"KEY": "value"},
    )

    assert isinstance(request.command, ShellParseResult)
    assert request.cwd == "/workspace"
    assert request.environment == {"KEY": "value"}
    assert request.input_data is None
    assert tuple(f.name for f in fields(request)) == (
        "command",
        "cwd",
        "environment",
        "input_data",
    )


def test_tool_request_carries_only_logical_bindings() -> None:
    request = _ToolExecutionRequest(
        code="greet()",
        cwd="/workspace",
        environment={},
        bindings=(_ToolBinding(name="greeter", path="tools/greeter"),),
    )

    assert request.code == "greet()"
    assert request.bindings == (_ToolBinding(name="greeter", path="tools/greeter"),)


def test_requests_have_no_backend_discriminator_fields() -> None:
    for fact in (_ShellExecutionRequest, _ToolExecutionRequest):
        assert "backend" not in {field.name for field in fields(fact)}
        assert "provider" not in {field.name for field in fields(fact)}
    assert tuple(f.name for f in fields(_ShellExecutionRequest)) == (
        "command",
        "cwd",
        "environment",
        "input_data",
    )
    assert tuple(f.name for f in fields(_ToolExecutionRequest)) == (
        "code",
        "cwd",
        "environment",
        "bindings",
    )


def test_filesystem_error_carries_neutral_kind() -> None:
    error = _FilesystemError("not_found", "no such file: /workspace/a.txt")

    assert error.kind == "not_found"
    assert str(error) == "no such file: /workspace/a.txt"


def test_prepare_is_synchronous_and_defers_resource_creation() -> None:
    async def scenario() -> None:
        workspace = _FakeBackendWorkspace()
        execution = workspace.prepare_shell(
            _ShellExecutionRequest(
                command=parse_shell_ast("echo hi"),
                cwd="/workspace",
                environment={},
            )
        )

        assert not asyncio.iscoroutine(execution)
        assert await execution.run(_NullOutput()) == ExitStatus(0)

    asyncio.run(scenario())


def test_fake_backend_workspace_runs_execution_and_filesystem_flows() -> None:
    async def scenario() -> None:
        workspace = await _FakeBackend().open_workspace(
            source=_WorkspaceSource(
                root=Path("/host"),
                environment=Path("/host/.workspace/env"),
            ),
            capability_source=_CapabilitySource(repertoire=Path("/host/repertoire")),
            capability_state=_CapabilityState(root=Path("/host/.workspace")),
        )
        result = await workspace.filesystem.write(
            _FileWriteRequest(path="/workspace/a.txt", content=b"hello")
        )
        assert result == _FileWriteResult(path="/workspace/a.txt", bytes_written=5)
        assert await workspace.filesystem.read("/workspace/a.txt") == b"hello"
        metadata = await workspace.filesystem.stat("/workspace/a.txt")
        assert (metadata.kind, metadata.size) == ("file", 5)
        assert await workspace.filesystem.edit(
            _FileEditRequest(
                path="/workspace/a.txt",
                edits=(_FileEdit(old_text="hello", new_text="world"),),
            )
        ) == _FileEditResult(path="/workspace/a.txt", blocks_replaced=1)
        assert await workspace.filesystem.read("/workspace/a.txt") == b"world"
        assert await workspace.reconcile_tool_runtime() == _ToolRuntimeStatus(
            available=True,
            error=None,
        )
        await workspace.flush()
        await workspace.close()

    asyncio.run(scenario())


def test_bound_capability_view_contract_needs_no_host_mechanics() -> None:
    view = _InMemoryCapabilityView()

    assert isinstance(view, _BoundCapabilityView)

    async def scenario() -> None:
        inspection = await view.inspect("tools/math.py")
        assert inspection.provenance == "repertoire"
        assert inspection.shadows_repertoire is False
        assert inspection.valid is True
        missing = await view.inspect("tools/unknown.py")
        assert missing.provenance is None
        assert [entry.name for entry in await view.list("tools")] == ["math.py"]
        assert await view.read("skills/review/SKILL.md") == b"# Review\n"
        assert (await view.stat("tools/math.py")).kind == "file"

    asyncio.run(scenario())


def _is_backend_neutral_type(annotation: object) -> bool:
    if annotation is type(None) or annotation is Ellipsis:
        return True
    origin = get_origin(annotation)
    if origin is not None:
        return all(_is_backend_neutral_type(arg) for arg in get_args(annotation))
    if isinstance(annotation, type):
        if annotation in _BACKEND_NEUTRAL_FACTS or annotation is ShellParseResult:
            return True
        return annotation.__module__ in {"builtins", "collections.abc", "typing"}
    return True
