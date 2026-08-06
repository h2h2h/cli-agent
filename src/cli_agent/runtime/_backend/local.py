"""Local Backend: Host-filesystem Workspace and Host subprocess mechanics.

The Local Backend is the reference RFC-0012 implementation: it owns the Host
``Path`` used for filesystem operations, the Host ambient environment merge
strategy, every ordinary Shell subprocess, and the file-level Capability View
materialization (symlink attach, copy-up, whiteouts, mutation lock), while
exposing only backend-neutral facts and contracts. Tool preparation and the
Workspace MCP Runtime arrive with later issues; the corresponding members
fail loudly with ``NotImplementedError`` until then.
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
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import AsyncIterator, Literal

from cli_agent.runtime._backend.edit import apply_edits
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
    _Provenance,
    _ResolvedPath,
    _ShellExecutionRequest,
    _ToolExecutionRequest,
    _ToolRuntimeStatus,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.protocol import (
    _BoundCapabilityView,
    _WorkspaceMCPRuntime,
)
from cli_agent.runtime._capability.command_parser import (
    FileRedirect,
    ShellParseResult,
    collect_redirects,
)
from cli_agent.runtime._capability.source import _CAPABILITY_DIRECTORIES
from cli_agent.runtime._capability.workspace import (
    _ensure_real_directory,
    _load_workspace_env,
)
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
                Host Capability lower input; the Local Bound Capability View
                is materialized from it during open.
            capability_state (`_CapabilityState`):
                Host persistent Capability state root; the materialized View
                and its whiteouts live under it.

        Returns:
            The opened Local Backend Workspace.

        Raises:
            ValueError: If the Workspace root or Repertoire is missing, or
                the environment file is unreadable.
        """

        root = source.root.resolve()
        if not root.is_dir():
            raise ValueError(f"workspace must be an existing directory: {root}")
        environment = _load_workspace_env(source.environment)
        view = _LocalCapabilityView.materialize(
            state_root=capability_state.root,
            repertoire=capability_source.repertoire,
        )
        return _LocalBackendWorkspace(root, environment, view)


class _LocalBackendWorkspace:
    """One live Local Backend Workspace on the Host filesystem."""

    def __init__(
        self,
        root: Path,
        environment: Mapping[str, str],
        capability_view: _LocalCapabilityView | None = None,
    ) -> None:
        self.root = str(root)
        self._root = root
        self.filesystem = _LocalWorkspaceFilesystem(root, capability_view)
        self.capabilities: _BoundCapabilityView = (
            capability_view
            if capability_view is not None
            else _UnimplementedCapabilityView()
        )
        self.mcp: _WorkspaceMCPRuntime = _UnimplementedMCPRuntime()
        self.workspace_environment = environment
        self._capability_view = capability_view
        self._closed = False

    def execution_base_environment(self) -> Mapping[str, str]:
        """Return the Local execution base environment for child processes.

        The Host ambient environment is merged under the Workspace
        environment, so a Workspace value overrides the same Host variable.
        Handlers must not read ``os.environ`` themselves.
        """

        return {**os.environ, **self.workspace_environment}

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
    leave the Workspace root. ``stat`` reports effective facts like POSIX
    ``stat(2)``, following symlinks; ``list`` reports raw entry kinds.
    """

    def __init__(
        self,
        root: Path,
        capability_view: _LocalCapabilityView | None = None,
    ) -> None:
        self._root = root
        self._capability_view = capability_view

    def resolve(self, path: str, cwd: str) -> _ResolvedPath:
        """Resolve one Local Backend path against a Session cwd without I/O."""

        target = _resolve_path(Path(cwd), path)
        return _ResolvedPath(
            path=str(target),
            within_workspace=_is_within(target, self._root),
        )

    async def stat(self, path: str) -> _FileMetadata:
        target = self._resolve(path)
        try:
            return _metadata(os.stat(target))
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
        target = self._prepare_target(request.path)
        try:
            if target.is_dir() and not target.is_symlink():
                raise _FilesystemError(
                    "is_directory", f"is a directory: {request.path}"
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write(target, request.content)
        except OSError as exc:
            raise _filesystem_error(request.path, exc) from exc
        except ValueError as exc:
            raise _FilesystemError("invalid_path", str(exc)) from exc
        return _FileWriteResult(path=request.path, bytes_written=len(request.content))

    async def edit(self, request: _FileEditRequest) -> _FileEditResult:
        target = self._prepare_target(request.path)
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise _filesystem_error(request.path, exc) from exc
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _FilesystemError(
                "invalid_content", "file is not valid UTF-8"
            ) from exc
        bom, text = _split_bom(text)
        line_ending = _detect_line_ending(text)
        try:
            updated = apply_edits(
                text.replace("\r\n", "\n"), request.edits, request.path
            )
        except ValueError as exc:
            raise _FilesystemError("edit_failed", str(exc)) from exc
        if line_ending == "\r\n":
            updated = updated.replace("\n", "\r\n")
        await self.write(
            _FileWriteRequest(
                path=request.path,
                content=(bom + updated).encode("utf-8"),
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

    def _prepare_target(self, path: str) -> Path:
        target = self._resolve(path)
        view = self._capability_view
        if view is not None:
            try:
                view.prepare_path(target)
            except ValueError as exc:
                raise _FilesystemError("invalid_path", str(exc)) from exc
        return target


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
        mutation: _LocalCapabilityView | None,
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


class _LocalCapabilityView:
    """Local file-level lower/upper Capability View materialized in Workspace.

    Implements the generic ``_BoundCapabilityView`` contract (``root``,
    ``inspect``, ``list``, ``read``, ``stat``) with Host file mechanics:
    exact lower symlinks, Workspace copy-up, persistent whiteouts, and the
    Shell mutation lock. ``prepare_path`` and ``prepare_shell`` are Local-only
    seams consumed by the Local Filesystem and Local Shell execution; the
    generic Backend contract never exposes them.
    """

    def __init__(self, state_root: Path, repertoire: Path) -> None:
        self.root = str(state_root)
        self._root = state_root
        self._repertoire = repertoire
        self._whiteouts = state_root / ".capability-view" / "whiteouts"
        self._mutation_lock = asyncio.Lock()

    @classmethod
    def materialize(
        cls,
        state_root: Path,
        repertoire: Path,
    ) -> _LocalCapabilityView:
        """Create the Local layout and attach the effective Capability View.

        Args:
            state_root (`Path`):
                The Workspace state directory (``.workspace``).
            repertoire (`Path`):
                The Host Repertoire lower tree.

        Returns:
            The materialized Local Bound Capability View.

        Raises:
            ValueError: If the Repertoire root is missing or a Workspace
                capability symbolic link is invalid.
        """

        if not repertoire.is_dir():
            raise ValueError(f"repertoire must be an existing directory: {repertoire}")
        view = cls(state_root, repertoire)
        view._prepare_layout()
        view._attach()
        return view

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        """Return trusted provenance and shadow facts for one view path."""

        relative = _managed_capability_path(relative_path)
        view_path = self._resolve_managed(relative_path)
        lower_path = self._repertoire / relative
        whiteout = self._whiteout_path(relative)

        if view_path.is_symlink():
            if not _is_exact_lower_link(view_path, lower_path):
                raise ValueError(
                    f"invalid Workspace capability symbolic link: {relative}"
                )
            provenance: _Provenance | None = "repertoire"
        elif _lexists(view_path):
            provenance = "workspace"
        elif whiteout.is_file():
            provenance = "whiteout"
        else:
            provenance = None

        valid = True
        validation_error = None
        if (
            provenance == "workspace"
            and _lexists(lower_path)
            and view_path.is_dir() != lower_path.is_dir()
        ):
            valid = False
            validation_error = (
                "Workspace override type does not match the Repertoire path"
            )

        return _CapabilityInspection(
            relative_path=relative.as_posix(),
            provenance=provenance,
            shadows_repertoire=(provenance == "workspace" and _lexists(lower_path)),
            valid=valid,
            validation_error=validation_error,
        )

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        """Return sorted effective entries for one managed directory."""

        relative = _managed_capability_path(relative_path)
        directory = self._resolve_managed(relative_path)
        try:
            with os.scandir(directory) as entries:
                listed = tuple(
                    _DirectoryEntry(
                        name=entry.name,
                        metadata=_metadata(
                            os.stat(
                                self._resolve_managed(
                                    (relative / entry.name).as_posix()
                                )
                            )
                        ),
                    )
                    for entry in entries
                )
        except OSError as exc:
            raise _filesystem_error(relative_path, exc) from exc
        return tuple(sorted(listed, key=lambda entry: entry.name))

    async def read(self, relative_path: str) -> bytes:
        """Read one managed file from the effective view."""

        target = self._resolve_managed(relative_path)
        try:
            return target.read_bytes()
        except OSError as exc:
            raise _filesystem_error(relative_path, exc) from exc

    async def stat(self, relative_path: str) -> _FileMetadata:
        """Return effective metadata for one managed path."""

        target = self._resolve_managed(relative_path)
        try:
            return _metadata(os.stat(target))
        except OSError as exc:
            raise _filesystem_error(relative_path, exc) from exc

    def prepare_path(self, path: Path) -> None:
        """Prepare one managed view path for a direct file mutation.

        Args:
            path (`Path`):
                The absolute target path a Runtime command is about to write.

        Raises:
            ValueError: If the path traverses a symbolic-link intermediate
                directory or is an invalid lower link.
        """

        if not self._is_in_view(path):
            return
        self._reject_symlink_intermediates(path)
        if path.is_symlink():
            self._copy_up(path)
            return
        if not _lexists(path):
            self._remove_whiteout(path.relative_to(self._root))

    @asynccontextmanager
    async def prepare_shell(
        self,
        command: ShellParseResult,
        cwd: Path,
        *,
        cancelled: Callable[[], bool],
    ) -> AsyncIterator[bool]:
        """Copy up output-redirected targets before one Shell command runs."""

        if not _may_mutate(command):
            yield not cancelled()
            return

        async with self._mutation_lock:
            if cancelled():
                yield False
                return
            for path in self._write_paths(command, cwd):
                self._copy_up(path)
            yield True

    def _resolve_managed(self, relative_path: str) -> Path:
        relative = _managed_capability_path(relative_path)
        target = self._root / relative
        self._reject_symlink_intermediates(target)
        if target.is_symlink():
            lower = self._repertoire / relative
            if not _is_exact_lower_link(target, lower) or not _is_regular_file(lower):
                raise ValueError(
                    f"invalid Workspace capability symbolic link: {relative}"
                )
        return target

    def _prepare_layout(self) -> None:
        _ensure_real_directory(self._root, label="workspace state path")
        _ensure_real_directory(
            self._root / ".capability-view",
            label="Capability View metadata path",
        )
        _ensure_real_directory(
            self._whiteouts,
            label="Capability View whiteout path",
        )
        for name in _CAPABILITY_DIRECTORIES:
            _ensure_real_directory(
                self._root / name,
                label=f"Workspace {name} capability path",
            )

    def _attach(self) -> None:
        for name in _CAPABILITY_DIRECTORIES:
            view_root = self._root / name
            lower_root = self._repertoire / name
            self._remove_stale_lower_links(view_root, lower_root)
            if lower_root.is_dir():
                self._attach_directory(view_root, lower_root, Path(name))

    def _remove_stale_lower_links(
        self,
        view_directory: Path,
        lower_directory: Path,
    ) -> None:
        for entry in tuple(view_directory.iterdir()):
            lower_entry = lower_directory / entry.name
            if entry.is_symlink():
                if not _is_exact_lower_link(entry, lower_entry):
                    raise ValueError(
                        "Workspace capability symbolic links must point to the "
                        f"matching Repertoire file: {entry}"
                    )
                if not lower_entry.is_file():
                    entry.unlink()
                continue
            if entry.is_dir():
                self._remove_stale_lower_links(entry, lower_entry)

    def _attach_directory(
        self,
        view_directory: Path,
        lower_directory: Path,
        relative_directory: Path,
    ) -> None:
        for lower_entry in sorted(
            lower_directory.iterdir(), key=lambda path: path.name
        ):
            mode = lower_entry.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ValueError(
                    f"Repertoire capability paths must not be symbolic links: "
                    f"{lower_entry}"
                )

            relative = relative_directory / lower_entry.name
            view_entry = view_directory / lower_entry.name
            if stat.S_ISDIR(mode):
                if _lexists(view_entry):
                    if view_entry.is_symlink():
                        raise ValueError(
                            "Workspace capability symbolic links must point to "
                            f"matching Repertoire files: {view_entry}"
                        )
                    if not view_entry.is_dir():
                        continue
                else:
                    try:
                        view_entry.mkdir()
                    except FileExistsError:
                        if not view_entry.is_dir():
                            continue
                self._attach_directory(view_entry, lower_entry, relative)
                continue

            if not stat.S_ISREG(mode):
                raise ValueError(
                    f"Repertoire capability paths must be regular files or "
                    f"directories: {lower_entry}"
                )

            whiteout = self._whiteout_path(relative)
            if whiteout.is_file():
                if _is_exact_lower_link(view_entry, lower_entry):
                    view_entry.unlink()
                elif _lexists(view_entry):
                    self._remove_whiteout(relative)
                continue

            if not _lexists(view_entry):
                try:
                    view_entry.symlink_to(lower_entry)
                except FileExistsError:
                    if not _is_exact_lower_link(view_entry, lower_entry):
                        raise ValueError(
                            "Workspace capability path changed while attaching: "
                            f"{view_entry}"
                        ) from None
            elif view_entry.is_symlink() and not _is_exact_lower_link(
                view_entry,
                lower_entry,
            ):
                raise ValueError(
                    "Workspace capability symbolic links must point to the "
                    f"matching Repertoire file: {view_entry}"
                )

    def _write_paths(
        self,
        command: ShellParseResult,
        cwd: Path,
    ) -> tuple[Path, ...]:
        targets = [
            redirect.target.value
            or redirect.target.quoted_content
            or redirect.target.text
            for redirect in collect_redirects(command.root)
            if isinstance(redirect, FileRedirect)
            and redirect.is_output
            and redirect.target is not None
        ]
        return self._normalize_targets(targets, cwd)

    def _normalize_targets(
        self,
        targets: list[str],
        cwd: Path,
    ) -> tuple[Path, ...]:
        normalized: list[Path] = []
        for target in targets:
            if not target or target == "-":
                continue
            if any(character in target for character in "*?[{~$`"):
                return tuple(self._root / name for name in _CAPABILITY_DIRECTORIES)
            candidate = Path(target)
            if not candidate.is_absolute():
                candidate = cwd / candidate
            lexical = Path(os.path.abspath(os.path.normpath(str(candidate))))
            if self._is_in_view(lexical):
                self._reject_symlink_intermediates(lexical)
                normalized.append(lexical)
        return tuple(dict.fromkeys(normalized))

    def _copy_up(self, path: Path) -> None:
        if path.is_symlink():
            lower_path = self._lower_for_view_path(path)
            if lower_path is None or not _is_exact_lower_link(path, lower_path):
                raise ValueError(f"invalid Workspace capability symbolic link: {path}")
            descriptor, temporary_name = tempfile.mkstemp(
                dir=path.parent,
                prefix=".cli-agent-copy-up-",
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                shutil.copy2(lower_path, temporary)
                os.replace(temporary, path)
            finally:
                if _lexists(temporary):
                    temporary.unlink()
            self._remove_whiteout(path.relative_to(self._root))
            return

        if path.is_dir():
            for child in tuple(path.rglob("*")):
                if child.is_symlink():
                    self._copy_up(child)

    def _is_in_view(self, path: Path) -> bool:
        return any(
            _is_relative_to(path, self._root / name) for name in _CAPABILITY_DIRECTORIES
        )

    def _lower_for_view_path(self, path: Path) -> Path | None:
        if not self._is_in_view(path):
            return None
        return self._repertoire / path.relative_to(self._root)

    def _reject_symlink_intermediates(self, path: Path) -> None:
        if self._root.is_symlink():
            raise ValueError(
                f"Capability View root must not be a symbolic link: {self._root}"
            )
        for name in _CAPABILITY_DIRECTORIES:
            view_root = self._root / name
            if not _is_relative_to(path, view_root):
                continue
            if view_root.is_symlink():
                raise ValueError(
                    "Capability View paths must not traverse symbolic "
                    f"link directories: {view_root}"
                )
            current = view_root
            for part in path.relative_to(view_root).parts[:-1]:
                current /= part
                if current.is_symlink():
                    raise ValueError(
                        "Capability View paths must not traverse symbolic "
                        f"link directories: {current}"
                    )
                if not _lexists(current):
                    break
            return

    def _whiteout_path(self, relative: Path) -> Path:
        return self._whiteouts / relative

    def _remove_whiteout(self, relative: Path) -> None:
        marker = self._whiteout_path(relative)
        if marker.is_file():
            marker.unlink()
        parent = marker.parent
        while parent != self._whiteouts:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


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


def _is_within(path: Path, directory: Path) -> bool:
    path_norm = os.path.normcase(os.path.normpath(str(path)))
    directory_norm = os.path.normcase(os.path.normpath(str(directory)))
    try:
        return os.path.commonpath([path_norm, directory_norm]) == directory_norm
    except ValueError:
        return False


def _managed_capability_path(path: str) -> Path:
    relative = Path(path)
    if (
        relative.is_absolute()
        or not relative.parts
        or relative.parts[0] not in _CAPABILITY_DIRECTORIES
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("capability path must be managed and relative")
    return relative


def _is_exact_lower_link(view_path: Path, lower_path: Path) -> bool:
    if not view_path.is_symlink():
        return False
    try:
        target = Path(os.readlink(view_path))
    except OSError:
        return False
    if not target.is_absolute():
        target = view_path.parent / target
    return Path(os.path.abspath(os.path.normpath(str(target)))) == Path(
        os.path.abspath(os.path.normpath(str(lower_path)))
    )


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _may_mutate(command: ShellParseResult) -> bool:
    return command.contains_output_redirection


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
        return _FilesystemError("not_found", f"No such file or directory: {path}")
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


def _split_bom(content: str) -> tuple[str, str]:
    """Return the leading BOM (if any) and the content without it."""

    if content.startswith("\ufeff"):
        return "\ufeff", content[1:]
    return "", content


def _detect_line_ending(content: str) -> str:
    """Return ``\\r\\n`` when the first newline is CRLF, else ``\\n``."""

    first_newline = content.find("\n")
    if first_newline > 0 and content[first_newline - 1] == "\r":
        return "\r\n"
    return "\n"


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
