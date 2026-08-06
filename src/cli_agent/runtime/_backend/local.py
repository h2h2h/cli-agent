"""Local Backend: Host-filesystem Workspace and Host subprocess mechanics.

The Local Backend is the reference RFC-0012 implementation: it owns the Host
``Path`` used for filesystem operations, the Host ambient environment merge
strategy, and every ordinary Shell subprocess, while exposing only
backend-neutral facts and contracts. Tool preparation and Capability binding
arrive with later issues; the corresponding members fail loudly with
``NotImplementedError`` until then.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shutil
import signal
import stat
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import AsyncContextManager, Literal, Protocol

from cli_agent.runtime._backend.facts import (
    _CapabilityInspection,
    _CapabilitySource,
    _CapabilityState,
    _DirectoryEntry,
    _FileEditRequest,
    _FileEditResult,
    _FileKind,
    _FileMetadata,
    _FilesystemError,
    _FileWriteRequest,
    _FileWriteResult,
    _MCPServerFacts,
    _ShellExecutionRequest,
    _ToolExecutionRequest,
    _ToolRuntimeStatus,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.protocol import (
    _BoundCapabilityView,
    _WorkspaceMCPRuntime,
)
from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._capability.workspace import _load_workspace_env
from cli_agent.runtime._environment.handlers.base import (
    _ExecutionOutcome,
    _ExecutionOutput,
    _PreparedExecution,
)

_ProcessSpawner = Callable[[], Awaitable[asyncio.subprocess.Process]]


class _LocalBackend:
    """Open one Host-filesystem Local Backend Workspace."""

    async def open_workspace(
        self,
        source: _WorkspaceSource,
        capability_source: _CapabilitySource,
        capability_state: _CapabilityState,
    ) -> _LocalBackendWorkspace:
        """Open the Local Workspace; any open failure must fail closed.

        Args:
            source (`_WorkspaceSource`):
                Host Workspace root and environment file.
            capability_source (`_CapabilitySource`):
                Host Capability lower input; bound by a later migration.
            capability_state (`_CapabilityState`):
                Host persistent Capability state; bound by a later migration.

        Returns:
            The opened Local Backend Workspace.

        Raises:
            ValueError: If the Workspace root is missing or its environment
                file is unreadable.
        """

        del capability_source, capability_state
        root = source.root.resolve()
        if not root.is_dir():
            raise ValueError(f"workspace must be an existing directory: {root}")
        environment = _load_workspace_env(source.environment)
        return _LocalBackendWorkspace(root, environment)


class _LocalBackendWorkspace:
    """One live Local Backend Workspace on the Host filesystem."""

    def __init__(self, root: Path, environment: Mapping[str, str]) -> None:
        self.root = str(root)
        self._root = root
        self.filesystem = _LocalWorkspaceFilesystem(root)
        self.capabilities: _BoundCapabilityView = _UnimplementedCapabilityView()
        self.mcp: _WorkspaceMCPRuntime = _UnimplementedMCPRuntime()
        self.workspace_environment = environment
        self._capability_view: _ShellMutation | None = None
        self._closed = False

    def execution_base_environment(self) -> Mapping[str, str]:
        """Return the Local execution base environment for child processes.

        The Host ambient environment is merged under the Workspace
        environment, so a Workspace value overrides the same Host variable.
        Handlers must not read ``os.environ`` themselves.
        """

        return {**os.environ, **self.workspace_environment}

    def bind_capability_view(self, view: _ShellMutation | None) -> None:
        """Temporarily attach the legacy Capability View for Shell copy-up.

        Removed when the Capability Source/State layering arrives.
        """

        self._capability_view = view

    def prepare_shell(
        self,
        request: _ShellExecutionRequest,
    ) -> _PreparedExecution:
        """Prepare one Shell execution without starting a subprocess."""

        return _LocalShellExecution(
            command=request.command,
            cwd=_resolve_path(self._root, request.cwd),
            environment={
                **self.execution_base_environment(),
                **request.environment,
            },
            mutation=self._capability_view,
        )

    def prepare_tool(
        self,
        request: _ToolExecutionRequest,
    ) -> _PreparedExecution:
        """Prepare one Tool worker execution; arrives with Handler migration."""

        del request
        raise NotImplementedError("Local Tool preparation is not implemented yet")

    async def reconcile_tool_runtime(self) -> _ToolRuntimeStatus:
        """Reconcile the Local Tool Runtime; arrives with Tool migration."""

        raise NotImplementedError("Local Tool Runtime reconcile is not implemented yet")

    async def flush(self) -> None:
        """Local Workspace changes are immediately durable; nothing to flush."""

    async def close(self) -> None:
        """Close this Workspace idempotently; no open resources yet."""

        self._closed = True


class _LocalWorkspaceFilesystem:
    """Host-filesystem-backed Workspace Filesystem.

    Paths are resolved against the Workspace root unless absolute; the Local
    Backend stays permissive like the current CLI, so absolute paths may
    leave the Workspace root.
    """

    def __init__(self, root: Path) -> None:
        self._root = root

    async def stat(self, path: str) -> _FileMetadata:
        target = self._resolve(path)
        try:
            return _metadata(os.lstat(target))
        except OSError as exc:
            raise _filesystem_error(path, exc) from exc

    async def list(self, path: str) -> tuple[_DirectoryEntry, ...]:
        target = self._resolve(path)
        try:
            with os.scandir(target) as entries:
                directory = tuple(
                    _DirectoryEntry(
                        name=entry.name,
                        metadata=_metadata(entry.stat(follow_symlinks=False)),
                    )
                    for entry in entries
                )
        except OSError as exc:
            raise _filesystem_error(path, exc) from exc
        return tuple(sorted(directory, key=lambda entry: entry.name))

    async def read(self, path: str) -> bytes:
        target = self._resolve(path)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise _filesystem_error(path, exc) from exc

    async def write(self, request: _FileWriteRequest) -> _FileWriteResult:
        target = self._resolve(request.path)
        try:
            if target.is_dir() and not target.is_symlink():
                raise _FilesystemError(
                    "is_directory", f"is a directory: {request.path}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, request.content)
        except OSError as exc:
            raise _filesystem_error(request.path, exc) from exc
        return _FileWriteResult(path=request.path, bytes_written=len(request.content))

    async def edit(self, request: _FileEditRequest) -> _FileEditResult:
        content = await self.read(request.path)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _FilesystemError(
                "internal", f"file is not valid UTF-8: {request.path}"
            ) from exc
        for edit in request.edits:
            if edit.old_text not in text:
                raise _FilesystemError(
                    "not_found", f"edit target not found: {request.path}"
                )
            text = text.replace(edit.old_text, edit.new_text, 1)
        await self.write(
            _FileWriteRequest(
                path=request.path,
                content=text.encode("utf-8"),
            )
        )
        return _FileEditResult(path=request.path, blocks_replaced=len(request.edits))

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        target = self._resolve(path)
        try:
            if target.is_dir() and not target.is_symlink():
                if not recursive:
                    raise _FilesystemError("is_directory", f"is a directory: {path}")
                shutil.rmtree(target)
            else:
                os.unlink(target)
        except OSError as exc:
            raise _filesystem_error(path, exc) from exc

    def _resolve(self, path: str) -> Path:
        return _resolve_path(self._root, path)


class _LocalShellExecution:
    """Run one ordinary Shell command inside the Local Backend Workspace.

    The subprocess is created only when :meth:`run` starts; cancellation
    before ``run`` never allocates a process. The optional ``mutation`` seam
    copies up output-redirected capability targets before the process spawns.
    """

    def __init__(
        self,
        command: ShellParseResult,
        cwd: Path,
        environment: Mapping[str, str],
        mutation: _ShellMutation | None,
    ) -> None:
        self._command = command
        self._cwd = cwd
        self._mutation = mutation
        self._cancel_requested = False
        self._process = _ProcessExecution(
            _shell_spawner(command.raw_command, cwd, environment)
        )

    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        mutation = self._mutation
        if mutation is None:
            return await self._process.run(output)
        async with mutation.prepare_shell(
            self._command,
            self._cwd,
            cancelled=lambda: self._cancel_requested,
        ) as prepared:
            if not prepared:
                return _ExecutionOutcome.killed()
            return await self._process.run(output)

    async def cancel(self) -> None:
        self._cancel_requested = True
        await self._process.cancel()


class _ProcessExecution:
    """Own one child process and its process group."""

    def __init__(
        self,
        spawn: _ProcessSpawner,
        *,
        input_data: bytes | None = None,
    ) -> None:
        self._spawn = spawn
        self._input_data = input_data
        self._process: asyncio.subprocess.Process | None = None
        self._ready = asyncio.Event()
        self._completed = asyncio.Event()
        self._run_started = False
        self._cancel_requested = False

    async def run(self, output: _ExecutionOutput) -> _ExecutionOutcome:
        self._run_started = True
        process: asyncio.subprocess.Process | None = None
        try:
            if self._cancel_requested:
                return _ExecutionOutcome.killed()

            process = await self._spawn()
            self._process = process
            self._ready.set()
            if self._cancel_requested:
                _signal_process(process, force=False)
            if self._input_data is not None:
                if process.stdin is None:
                    raise RuntimeError("process input was configured without stdin")
                process.stdin.write(self._input_data)
                await process.stdin.drain()
                process.stdin.close()

            stdout_task = asyncio.create_task(
                self._capture_stream(output, process.stdout, "stdout")
            )
            stderr_task = asyncio.create_task(
                self._capture_stream(output, process.stderr, "stderr")
            )
            exit_code = await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            if self._cancel_requested:
                return _ExecutionOutcome.killed(exit_code)
            if exit_code == 0:
                return _ExecutionOutcome.exited(exit_code)
            return _ExecutionOutcome.failed(exit_code)
        except Exception:
            if process is not None:
                _signal_process(process, force=True)
                with suppress(Exception):
                    await process.wait()
            exit_code = process.returncode if process is not None else None
            if self._cancel_requested:
                return _ExecutionOutcome.killed(exit_code)
            return _ExecutionOutcome.failed(exit_code)
        finally:
            self._ready.set()
            self._completed.set()

    async def cancel(self) -> None:
        self._cancel_requested = True
        if not self._run_started:
            return
        await self._ready.wait()
        process = self._process
        if process is None or process.returncode is not None:
            return

        _signal_process(process, force=False)
        try:
            await asyncio.wait_for(
                self._completed.wait(),
                timeout=0.5,
            )
        except asyncio.TimeoutError:
            _signal_process(process, force=True)

    async def _capture_stream(
        self,
        output: _ExecutionOutput,
        stream: asyncio.StreamReader | None,
        stream_name: Literal["stdout", "stderr"],
    ) -> None:
        if stream is None:
            return
        while data := await stream.read(4096):
            await output.write(stream_name, data)


class _ShellMutation(Protocol):
    """Temporary Shell redirect copy-up seam replaced by Capability layering."""

    def prepare_shell(
        self,
        command: ShellParseResult,
        cwd: Path,
        *,
        cancelled: Callable[[], bool],
    ) -> AsyncContextManager[bool]:
        """Return a context manager that copies up before one Shell runs."""
        ...


class _UnimplementedCapabilityView:
    """Bound Capability View placeholder bound by a later migration."""

    @property
    def root(self) -> str:
        raise NotImplementedError("Bound Capability View is not implemented yet")

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        raise NotImplementedError("Bound Capability View is not implemented yet")

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        raise NotImplementedError("Bound Capability View is not implemented yet")

    async def read(self, relative_path: str) -> bytes:
        raise NotImplementedError("Bound Capability View is not implemented yet")

    async def stat(self, relative_path: str) -> _FileMetadata:
        raise NotImplementedError("Bound Capability View is not implemented yet")


class _UnimplementedMCPRuntime:
    """Workspace MCP Runtime placeholder bound by a later migration."""

    async def discover(self) -> tuple[_MCPServerFacts, ...]:
        raise NotImplementedError("Workspace MCP Runtime is not implemented yet")


def _resolve_path(root: Path, path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return Path(os.path.abspath(os.path.normpath(str(candidate))))


def _shell_spawner(
    raw_command: str,
    cwd: Path,
    environment: Mapping[str, str],
) -> _ProcessSpawner:
    """Return a spawner that starts one Shell process in the Workspace."""

    async def spawn() -> asyncio.subprocess.Process:
        return await asyncio.create_subprocess_shell(
            raw_command,
            cwd=cwd,
            env=dict(environment),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )

    return spawn


def _signal_process(
    process: asyncio.subprocess.Process,
    *,
    force: bool,
) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(
                process.pid,
                signal.SIGKILL if force else signal.SIGTERM,
            )
        elif force:
            process.kill()
        else:
            process.terminate()


def _metadata(info: os.stat_result) -> _FileMetadata:
    if stat.S_ISLNK(info.st_mode):
        kind: _FileKind = "symlink"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    return _FileMetadata(
        kind=kind,
        size=info.st_size,
        mtime_ns=info.st_mtime_ns,
        mode=stat.S_IMODE(info.st_mode),
    )


def _filesystem_error(path: str, exc: OSError) -> _FilesystemError:
    error = exc.errno
    if error == errno.ENOENT:
        return _FilesystemError("not_found", f"no such path: {path}")
    if error == errno.ENOTDIR:
        return _FilesystemError(
            "not_a_directory", f"path component is not a directory: {path}"
        )
    if error == errno.EISDIR:
        return _FilesystemError("is_directory", f"is a directory: {path}")
    if error == errno.EACCES:
        return _FilesystemError("permission_denied", f"permission denied: {path}")
    if error == errno.EEXIST:
        return _FilesystemError("already_exists", f"path already exists: {path}")
    return _FilesystemError("internal", f"filesystem error for {path}: {exc}")


def _atomic_write(path: Path, content: bytes) -> None:
    """Atomically replace one file, preserving its mode when present."""

    try:
        mode = stat.S_IMODE(path.stat().st_mode) if os.path.lexists(path) else None
    except OSError:
        mode = None
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".cli-agent-write-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), 0o644 if mode is None else mode)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            with suppress(OSError):
                temporary.unlink()
