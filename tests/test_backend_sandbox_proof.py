"""Issue 11: deterministic non-Host-mirror Backend proof.

This module defines a pure in-memory Sandbox Backend that shares one
``/sandbox`` namespace between Shell, Files, Tools, and every Capability
Catalog, without reading or writing any Host path. The proofs run the real
Runtime, Handlers, Catalogs, and Library worker against it, showing that
RFC-0012 decoupling works without a Host mirror, provenance needs no
symlinks, and Backend open failure never falls back to Local.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import posixpath
import re
import sys
import types
from collections.abc import Mapping
from types import SimpleNamespace

import pytest
from interaction_fakes import _ScriptedInteraction

import cli_agent.runtime._workspace as workspace_module
from cli_agent.runtime import (
    AgentRuntime,
    AssistantMessage,
    ContextPolicy,
    ModelCompletion,
    ScriptedModelProvider,
    ToolCall,
    ToolResult,
    UserMessage,
)
from cli_agent.runtime._backend.edit import apply_edits
from cli_agent.runtime._backend.facts import (
    _CapabilityInspection,
    _DirectoryEntry,
    _FileEditRequest,
    _FileEditResult,
    _FileMetadata,
    _FilesystemError,
    _FileWriteRequest,
    _FileWriteResult,
    _ResolvedPath,
    _ToolRuntimeStatus,
)
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._database.state import _StateDatabase
from cli_agent.runtime._database.summary_cache import _SummaryCache
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExecutionHandle,
    ExecutionOutputSink,
    ExitStatus,
)

_SANDBOX_ROOT = "/sandbox"
_VIEW_ROOT = "/sandbox/.workspace"
_CAPABILITY_DIRECTORIES = ("tools", "skills", "library", "_mcp")

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)

_MARKER_TOOL = (
    "def write(name, text):\n"
    "    Path(name).write_text(text)\n"
    "    return 'ok'\n"
    "\n"
    "def read(name):\n"
    "    return Path(name).read_text()\n"
)

_REVIEW_SKILL = (
    "---\nname: review\ndescription: Review the codebase.\n---\n\n# Review\n"
)


def _resolve(path: str, cwd: str) -> str:
    candidate = path if path.startswith("/") else posixpath.join(cwd, path)
    return posixpath.normpath(candidate)


def _within_workspace(path: str) -> bool:
    return path == _SANDBOX_ROOT or path.startswith(_SANDBOX_ROOT + "/")


def _has_children(
    files: Mapping[str, bytes],
    dirs: set[str],
    directory: str,
) -> bool:
    prefix = directory.rstrip("/") + "/"
    return any(key.startswith(prefix) for key in files) or any(
        key.startswith(prefix) for key in dirs
    )


class _SandboxFilesystem:
    """In-memory Workspace Filesystem sharing the Sandbox namespace."""

    def __init__(self, files: dict[str, bytes], dirs: set[str]) -> None:
        self._files = files
        self._dirs = dirs

    def resolve(self, path: str, cwd: str) -> _ResolvedPath:
        target = _resolve(path, cwd)
        return _ResolvedPath(
            path=target,
            within_workspace=_within_workspace(target),
        )

    async def stat(self, path: str) -> _FileMetadata:
        target = _resolve(path, _SANDBOX_ROOT)
        if target in self._files:
            return _FileMetadata(
                kind="file",
                size=len(self._files[target]),
                mtime_ns=0,
                mode=0o644,
            )
        if target in self._dirs or _has_children(self._files, self._dirs, target):
            return _FileMetadata(
                kind="directory",
                size=0,
                mtime_ns=0,
                mode=0o700,
            )
        raise _FilesystemError("not_found", f"No such file or directory: {path}")

    async def list(self, path: str) -> tuple[_DirectoryEntry, ...]:
        target = _resolve(path, _SANDBOX_ROOT)
        names = _entries_beneath(self._files, self._dirs, target)
        entries = await asyncio.gather(
            *(self._entry(posixpath.join(target, name)) for name in names)
        )
        return tuple(sorted(entries, key=lambda entry: entry.name))

    async def _entry(self, path: str) -> _DirectoryEntry:
        return _DirectoryEntry(
            name=posixpath.basename(path),
            metadata=await self.stat(path),
        )

    async def read(self, path: str) -> bytes:
        target = _resolve(path, _SANDBOX_ROOT)
        try:
            return self._files[target]
        except KeyError:
            raise _FilesystemError(
                "not_found", f"No such file or directory: {path}"
            ) from None

    async def write(self, request: _FileWriteRequest) -> _FileWriteResult:
        self._files[_resolve(request.path, _SANDBOX_ROOT)] = request.content
        return _FileWriteResult(
            path=request.path,
            bytes_written=len(request.content),
        )

    async def edit(self, request: _FileEditRequest) -> _FileEditResult:
        content = await self.read(request.path)
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise _FilesystemError(
                "invalid_content", "file is not valid UTF-8"
            ) from exc
        try:
            updated = apply_edits(text, request.edits, request.path)
        except ValueError as exc:
            raise _FilesystemError("edit_failed", str(exc)) from exc
        await self.write(
            _FileWriteRequest(
                path=request.path,
                content=updated.encode("utf-8"),
            )
        )
        return _FileEditResult(path=request.path, blocks_replaced=len(request.edits))

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        target = _resolve(path, _SANDBOX_ROOT)
        if target in self._files:
            self._files.pop(target, None)
            return
        if target in self._dirs or _has_children(self._files, self._dirs, target):
            if not recursive:
                raise _FilesystemError("is_directory", f"is a directory: {path}")
            prefix = target.rstrip("/") + "/"
            for key in tuple(self._files):
                if key.startswith(prefix):
                    self._files.pop(key, None)
            for key in tuple(self._dirs):
                if key.startswith(prefix):
                    self._dirs.discard(key)
            return
        raise _FilesystemError("not_found", f"No such file or directory: {path}")


def _entries_beneath(
    files: Mapping[str, bytes],
    dirs: set[str],
    directory: str,
) -> set[str]:
    prefix = directory.rstrip("/") + "/"
    names: set[str] = set()
    for key in files:
        if key.startswith(prefix):
            name = key[len(prefix) :].split("/", 1)[0]
            if name:
                names.add(name)
    for key in dirs:
        if key.startswith(prefix) and key != prefix.rstrip("/"):
            name = key[len(prefix) :].split("/", 1)[0]
            if name:
                names.add(name)
    return names


class _SandboxCapabilityView:
    """In-memory Bound Capability View; provenance needs no symlinks."""

    root = _VIEW_ROOT

    def __init__(
        self,
        files: dict[str, bytes],
        dirs: set[str],
        provenance: dict[str, str],
        lower: frozenset[str],
    ) -> None:
        self._files = files
        self._dirs = dirs
        self._provenance = provenance
        self._lower = lower

    def _key(self, relative_path: str) -> str:
        return posixpath.join(_VIEW_ROOT, relative_path)

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        provenance = self._provenance.get(relative_path)
        return _CapabilityInspection(
            relative_path=relative_path,
            provenance=provenance,
            shadows_repertoire=(
                provenance == "workspace" and relative_path in self._lower
            ),
            valid=True,
            validation_error=None,
        )

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        target = self._key(relative_path)
        names = _entries_beneath(self._files, self._dirs, target)
        entries = await asyncio.gather(
            *(self._view_entry(relative_path, name) for name in names)
        )
        return tuple(sorted(entries, key=lambda entry: entry.name))

    async def _view_entry(
        self,
        relative_path: str,
        name: str,
    ) -> _DirectoryEntry:
        return _DirectoryEntry(
            name=name,
            metadata=await self.stat(posixpath.join(relative_path, name)),
        )

    async def read(self, relative_path: str) -> bytes:
        key = self._key(relative_path)
        try:
            return self._files[key]
        except KeyError:
            raise _FilesystemError(
                "not_found", f"No such file or directory: {relative_path}"
            ) from None

    async def stat(self, relative_path: str) -> _FileMetadata:
        key = self._key(relative_path)
        if key in self._files:
            return _FileMetadata(
                kind="file",
                size=len(self._files[key]),
                mtime_ns=0,
                mode=0o644,
            )
        if key in self._dirs or _has_children(self._files, self._dirs, key):
            return _FileMetadata(
                kind="directory",
                size=0,
                mtime_ns=0,
                mode=0o700,
            )
        raise _FilesystemError(
            "not_found", f"No such file or directory: {relative_path}"
        )


class _SandboxShellExecution:
    """Deterministic in-memory Shell: echo/cat/ls/mkdir/rm/pwd/sleep."""

    def __init__(
        self,
        files: dict[str, bytes],
        dirs: set[str],
        cwd: str,
        raw_command: str,
    ) -> None:
        self._files = files
        self._dirs = dirs
        self._cwd = cwd
        self._raw = raw_command.strip()
        self._cancelled = False

    async def run(self, output: ExecutionOutputSink) -> ExitStatus:
        if self._cancelled:
            return ExitStatus(_KILLED_BEFORE_START)
        return await _run_sandbox_shell(self, output)

    async def kill(self) -> None:
        self._cancelled = True


async def _run_sandbox_shell(
    execution: _SandboxShellExecution,
    output: ExecutionOutputSink,
) -> ExitStatus:
    raw = execution._raw
    try:
        if not raw:
            return ExitStatus(0)
        if raw == "pwd":
            await output.write("stdout", (execution._cwd + "\n").encode("utf-8"))
            return ExitStatus(0)
        if raw.startswith("echo "):
            return await _sandbox_echo(execution, output, raw)
        if raw.startswith("cat "):
            return await _sandbox_cat(execution, output, raw)
        if raw.startswith("ls"):
            return await _sandbox_ls(execution, output, raw)
        if raw.startswith("mkdir "):
            execution._dirs.add(_resolve(raw[6:].strip(), execution._cwd))
            return ExitStatus(0)
        if raw.startswith("rm "):
            await execution._files.pop(_resolve(raw[3:].strip(), execution._cwd), None)
            return ExitStatus(0)
        if raw.startswith("sleep "):
            return await _sandbox_sleep(execution, output, raw)
    except Exception as exc:
        await output.write("stderr", f"sandbox: {exc}\n".encode("utf-8"))
        return ExitStatus(1)
    await output.write(
        "stderr",
        f"sandbox: unsupported shell command: {raw}\n".encode("utf-8"),
    )
    return ExitStatus(1)


async def _sandbox_echo(
    execution: _SandboxShellExecution,
    output: ExecutionOutputSink,
    raw: str,
) -> ExitStatus:
    append = ">>" in raw
    text, _, redirect = raw.partition(">>" if append else ">")
    text = text.removeprefix("echo ").strip().strip("'\"")
    if not redirect:
        await output.write("stdout", (text + "\n").encode("utf-8"))
        return ExitStatus(0)
    path = _resolve(redirect.strip().strip("'\""), execution._cwd)
    content = (text + "\n").encode("utf-8")
    if append:
        execution._files[path] = execution._files.get(path, b"") + content
    else:
        execution._files[path] = content
    return ExitStatus(0)


async def _sandbox_cat(
    execution: _SandboxShellExecution,
    output: ExecutionOutputSink,
    raw: str,
) -> ExitStatus:
    path = _resolve(raw[4:].strip().strip("'\""), execution._cwd)
    try:
        content = execution._files[path]
    except KeyError:
        await output.write("stderr", f"cat: no such file: {path}\n".encode("utf-8"))
        return ExitStatus(1)
    await output.write("stdout", content)
    return ExitStatus(0)


async def _sandbox_ls(
    execution: _SandboxShellExecution,
    output: ExecutionOutputSink,
    raw: str,
) -> ExitStatus:
    path = raw[3:].strip() or execution._cwd
    target = _resolve(path.strip("'\""), execution._cwd)
    for name in sorted(_entries_beneath(execution._files, execution._dirs, target)):
        await output.write("stdout", (name + "\n").encode("utf-8"))
    return ExitStatus(0)


async def _sandbox_sleep(
    execution: _SandboxShellExecution,
    output: ExecutionOutputSink,
    raw: str,
) -> ExitStatus:
    del output
    seconds = float(raw[6:].strip())
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if execution._cancelled:
            return ExitStatus(_KILLED_BEFORE_START)
        await asyncio.sleep(0.01)
    return ExitStatus(0)


class _SandboxPath:
    """pathlib.Path-like facade over the in-memory Sandbox namespace."""

    def __init__(
        self,
        value: str,
        *,
        files: dict[str, bytes],
        cwd: str,
    ) -> None:
        self._files = files
        self._path = _resolve(str(value), cwd)

    def __truediv__(self, part: object) -> _SandboxPath:
        return _SandboxPath(
            posixpath.join(self._path, str(part)),
            files=self._files,
            cwd=_SANDBOX_ROOT,
        )

    def write_text(
        self,
        text: str,
        encoding: str = "utf-8",
        errors: str = "strict",
    ) -> None:
        del errors
        self._files[self._path] = text.encode(encoding)

    def read_text(
        self,
        encoding: str = "utf-8",
        errors: str = "strict",
    ) -> str:
        del errors
        return self._files[self._path].decode(encoding)

    def exists(self) -> bool:
        return self._path in self._files

    def touch(self) -> None:
        self._files.setdefault(self._path, b"")

    def __str__(self) -> str:
        return self._path

    def __repr__(self) -> str:
        return f"_SandboxPath({self._path!r})"


def _sandbox_path_factory(
    files: dict[str, bytes],
    cwd: str,
) -> object:
    def path(value: object) -> _SandboxPath:
        return _SandboxPath(str(value), files=files, cwd=cwd)

    return path


class _SandboxToolExecution:
    """In-process deterministic Tool worker over the Sandbox namespace."""

    def __init__(
        self,
        files: dict[str, bytes],
        view_root: str,
        cwd: str,
        request: object,
    ) -> None:
        self._files = files
        self._view_root = view_root
        self._cwd = cwd
        self._request = request
        self._cancelled = False

    async def run(self, output: ExecutionOutputSink) -> ExitStatus:
        if self._cancelled:
            return ExitStatus(_KILLED_BEFORE_START)
        try:
            result = _run_sandbox_code(
                self._request.code,
                self._files,
                self._view_root,
                self._cwd,
                self._request.bindings,
            )
        except Exception as exc:
            await output.write(
                "stderr", f"{type(exc).__name__}: {exc}\n".encode("utf-8")
            )
            return ExitStatus(1)
        if result is not None:
            await output.write("stdout", f"{result}\n".encode("utf-8"))
        return ExitStatus(0)

    async def kill(self) -> None:
        self._cancelled = True


def _run_sandbox_code(
    code: str,
    files: dict[str, bytes],
    view_root: str,
    cwd: str,
    bindings: tuple[object, ...],
) -> object:
    tools = SimpleNamespace()
    path_factory = _sandbox_path_factory(files, cwd)
    for binding in bindings:
        key = posixpath.join(view_root, binding.path)
        source = files[key].decode("utf-8")
        module = types.ModuleType(f"cli_agent_tool_{binding.name}")
        module.__dict__["__name__"] = f"cli_agent_tool_{binding.name}"
        module.__dict__["Path"] = path_factory
        exec(compile(source, binding.path, "exec"), module.__dict__)
        setattr(tools, binding.name, module)
    namespace: dict[str, object] = {
        "__name__": "__main__",
        "__builtins__": __builtins__,
        "cwd": cwd,
        "tools": tools,
        "Path": path_factory,
        "ast": ast,
        "json": json,
        "os": os,
        "re": re,
        "sys": sys,
    }
    try:
        return eval(compile(code, "<tools run>", "eval"), namespace)
    except SyntaxError:
        exec(compile(code, "<tools run>", "exec"), namespace)
        return None


class _SandboxMCPRuntime:
    """Empty Workspace MCP Runtime; discovery is a no-op in the Sandbox."""

    async def discover(
        self,
        configs: tuple[object, ...],
        on_diagnostic: object = None,
    ) -> tuple[object, ...]:
        del configs, on_diagnostic
        return ()

    async def materialize_binding(self, configs: tuple[object, ...]) -> None:
        del configs
        return None


class _SandboxBackendWorkspace:
    """One live Sandbox Workspace sharing the in-memory namespace."""

    def __init__(
        self,
        files: dict[str, bytes],
        dirs: set[str],
        provenance: dict[str, str],
        lower: frozenset[str],
    ) -> None:
        self.root = _SANDBOX_ROOT
        self._files = files
        self._dirs = dirs
        self.filesystem = _SandboxFilesystem(files, dirs)
        self.capabilities = _SandboxCapabilityView(
            files,
            dirs,
            provenance,
            lower,
        )
        self.mcp = _SandboxMCPRuntime()
        self.workspace_environment: Mapping[str, str] = {}
        self.closed = False

    def prepare_shell(
        self,
        request: object,
    ) -> ExecutionHandle:
        return _SandboxShellExecution(
            self._files,
            self._dirs,
            request.cwd,
            request.command.raw_command,
        )

    def prepare_tool(
        self,
        request: object,
    ) -> ExecutionHandle:
        return _SandboxToolExecution(
            self._files,
            self.capabilities.root,
            request.cwd,
            request,
        )

    async def reconcile_tool_runtime(self) -> _ToolRuntimeStatus:
        return _ToolRuntimeStatus(available=True, error=None)

    async def flush(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True


class _SandboxBackend:
    """Open one in-memory Sandbox Workspace; never reads the Host filesystem.

    Class-level seeds are deterministic per test; ``fail_open`` models a
    mandatory constraint failure that must never fall back to Local.
    """

    content: tuple[tuple[str, str, str], ...] = ()
    lower: frozenset[str] = frozenset()
    fail_open: bool = False

    @classmethod
    def seed(
        cls,
        relative_path: str,
        content: str,
        *,
        provenance: str = "repertoire",
    ) -> None:
        cls.content = (*cls.content, (relative_path, content, provenance))

    async def open_workspace(
        self,
        source: object,
        capability_source: object,
        capability_state: object,
    ) -> _SandboxBackendWorkspace:
        del source, capability_source, capability_state
        if self.fail_open:
            raise ValueError("sandbox constraint failed")
        files: dict[str, bytes] = {}
        provenance: dict[str, str] = {}
        for relative_path, content, prov in self.content:
            files[posixpath.join(_VIEW_ROOT, relative_path)] = content.encode("utf-8")
            provenance[relative_path] = prov
        dirs = {posixpath.join(_VIEW_ROOT, name) for name in _CAPABILITY_DIRECTORIES}
        return _SandboxBackendWorkspace(
            files,
            dirs,
            provenance,
            frozenset(self.lower),
        )


def _sandbox_workspace(
    seeds: tuple[tuple[str, str, str], ...] = (),
    *,
    lower: frozenset[str] = frozenset(),
) -> _SandboxBackendWorkspace:
    files: dict[str, bytes] = {}
    provenance: dict[str, str] = {}
    for relative_path, content, prov in seeds:
        files[posixpath.join(_VIEW_ROOT, relative_path)] = content.encode("utf-8")
        provenance[relative_path] = prov
    dirs = {posixpath.join(_VIEW_ROOT, name) for name in _CAPABILITY_DIRECTORIES}
    return _SandboxBackendWorkspace(
        files,
        dirs,
        provenance,
        frozenset(lower),
    )


def _completion(text: str) -> ModelCompletion:
    return ModelCompletion(message=AssistantMessage.text(text), finish_reason="stop")


@pytest.fixture(autouse=True)
def _reset_sandbox_backend() -> None:
    _SandboxBackend.content = ()
    _SandboxBackend.lower = frozenset()
    _SandboxBackend.fail_open = False


def test_full_runtime_runs_on_the_sandbox_backend(
    tmp_path: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SandboxBackend.seed("tools/marker.py", _MARKER_TOOL)
    _SandboxBackend.seed("skills/review/SKILL.md", _REVIEW_SKILL)
    monkeypatch.setattr(workspace_module, "_LocalBackend", _SandboxBackend)

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=((_completion("hi"),),)),
            context_policy=_context_policy,
        )
        assert isinstance(runtime._resources.backend, _SandboxBackendWorkspace)
        assert runtime._resources.tool_catalog.get("marker") is not None
        assert runtime._resources.tool_catalog.get("marker").valid
        assert runtime._resources.skill_catalog.get("review") is not None

        async for _ in runtime.run_turn("session-a", UserMessage.text("hi")):
            pass
        kernel = next(iter(runtime._sessions.values())).kernel
        files = runtime._resources.backend._files
        try:
            shell = _output(
                await _exec(kernel, "echo from-shell > shared/from-shell.txt")
            )
            assert shell["status"] == "exited"
            assert files["/sandbox/shared/from-shell.txt"] == b"from-shell\n"

            written = _output(
                await _exec(
                    kernel,
                    "files write shared/from-files.txt",
                    stdin="files text\n",
                )
            )
            assert written["status"] == "exited"
            assert files["/sandbox/shared/from-files.txt"] == b"files text\n"

            tool = _output(
                await _exec(
                    kernel,
                    "tools run \"tools.marker.write('shared/from-tool.txt', 'tool text')\"",
                )
            )
            assert tool["status"] == "exited"
            assert _text(tool, "stdout") == "ok\n"
            assert files["/sandbox/shared/from-tool.txt"] == b"tool text"

            tool_read = _text(
                _output(await _exec(kernel, "cat shared/from-tool.txt")),
                "stdout",
            )
            assert tool_read == "tool text"
            files_read = _text(
                _output(await _exec(kernel, "cat shared/from-files.txt")),
                "stdout",
            )
            assert files_read == "files text\n"
            shell_read = _text(
                _output(
                    await _exec(
                        kernel,
                        "tools run \"tools.marker.read('shared/from-shell.txt')\"",
                    )
                ),
                "stdout",
            )
            assert shell_read == "from-shell\n\n"
        finally:
            await runtime.close()

    asyncio.run(scenario())


def test_sandbox_two_kernels_share_files_but_not_cwd_env_or_handle() -> None:
    workspace = _sandbox_workspace(())

    async def scenario() -> None:
        first = EnvironmentKernel("/sandbox", backend=workspace)
        second = EnvironmentKernel("/sandbox", backend=workspace)
        try:
            await _exec(first, "mkdir sub")
            await _exec(first, "cd sub")
            pwd = _text(_output(await _exec(first, "pwd")), "stdout")
            assert pwd.strip() == "/sandbox/sub"
            await _exec(first, "export A_ONLY=yes")
            await _exec(first, "echo a > a.txt")

            other_pwd = _text(_output(await _exec(second, "pwd")), "stdout")
            assert other_pwd.strip() == "/sandbox"
            other_env = _text(_output(await _exec(second, "export")), "stdout")
            assert "A_ONLY" not in other_env
            shared = _text(_output(await _exec(second, "cat sub/a.txt")), "stdout")
            assert shared == "a\n"

            running = _output(await _exec(first, "sleep 5", wait_ms=0))
            assert running["status"] == "running"
            foreign = await second.dispatch(
                ToolCall(
                    call_id="foreign",
                    name="output",
                    arguments={"exec_id": running["exec_id"]},
                )
            )
            assert foreign.error is not None
            assert foreign.error["code"] == "unknown_execution"
        finally:
            await first.close()
            await second.close()

    asyncio.run(scenario())


def test_sandbox_library_worker_writes_indexes_into_the_namespace(
    tmp_path,
) -> None:
    workspace = _sandbox_workspace(
        (
            ("library/first.md", "first content\n", "repertoire"),
            ("library/second.txt", "second content\n", "repertoire"),
        )
    )
    cache = _SummaryCache(_StateDatabase.open(tmp_path / "state.sqlite3"))
    provider = ScriptedModelProvider(
        script=(
            (_completion("Summary of first."),),
            (_completion("Summary of second."),),
            (_completion("Library summary."),),
        )
    )

    async def scenario() -> None:
        catalog = await _LibraryCatalog.reconcile(
            workspace.capabilities,
            workspace.filesystem,
            cache,
        )
        catalog.start(provider)
        await catalog._queue.join()

        index = await workspace.capabilities.read("library/index.md")
        assert b"Summary of first." in index
        assert b"Summary of second." in index
        assert b"status: ready" in index

        await catalog.close()

    asyncio.run(scenario())


def test_sandbox_bound_view_provenance_needs_no_symlinks() -> None:
    workspace = _sandbox_workspace(
        (
            ("tools/lower.py", "VALUE = 1\n", "repertoire"),
            ("tools/upper.py", "VALUE = 2\n", "workspace"),
            ("tools/whiteouted.py", "", "whiteout"),
        ),
        lower=frozenset({"tools/lower.py", "tools/upper.py"}),
    )

    async def scenario() -> None:
        view = workspace.capabilities
        lower = await view.inspect("tools/lower.py")
        assert lower.provenance == "repertoire"
        assert lower.shadows_repertoire is False
        upper = await view.inspect("tools/upper.py")
        assert upper.provenance == "workspace"
        assert upper.shadows_repertoire is True
        missing = await view.inspect("tools/none.py")
        assert missing.provenance is None
        whiteout = await view.inspect("tools/whiteouted.py")
        assert whiteout.provenance == "whiteout"

    asyncio.run(scenario())


def test_sandbox_kill_terminates_a_running_shell() -> None:
    workspace = _sandbox_workspace(())

    async def scenario() -> None:
        kernel = EnvironmentKernel("/sandbox", backend=workspace)
        try:
            running = _output(await _exec(kernel, "sleep 5", wait_ms=0))
            assert running["status"] == "running"
            killed = _output(
                await kernel.dispatch(
                    ToolCall(
                        call_id="kill",
                        name="kill",
                        arguments={"exec_id": running["exec_id"]},
                    )
                )
            )
            assert killed["status"] == "killed"
            assert killed["is_terminal"] is True
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_sandbox_backend_open_failure_does_not_fall_back_to_local(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _SandboxBackend.fail_open = True
    monkeypatch.setattr(workspace_module, "_LocalBackend", _SandboxBackend)

    with pytest.raises(ValueError, match="sandbox constraint failed"):
        asyncio.run(
            AgentRuntime.open(
                user_interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
                context_policy=_context_policy,
            )
        )


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


def _text(snapshot: dict[str, object], stream: str) -> str:
    chunks = snapshot["chunks"]
    assert isinstance(chunks, list)
    return "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == stream
    )
