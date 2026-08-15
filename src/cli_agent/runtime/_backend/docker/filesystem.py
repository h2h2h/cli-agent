"""Docker volume Workspace Filesystem implementation.

Filesystem operations execute inside one long-lived helper container that
mounts the durable Workspace volume at the container-native root. Every
operation is one ``python3 -c`` exec carrying a JSON request on stdin and
returning a JSON response on stdout, so binary content travels as base64
and no Host path is ever mapped into the container namespace.

The helper script is self-contained (stdlib only) and embedded here: it is
executed inside the container image and cannot import ``cli_agent``.
"""

from __future__ import annotations

import base64
import posixpath
from typing import TYPE_CHECKING

from cli_agent.runtime._backend.edit import _detect_line_ending, _split_bom, apply_edits
from cli_agent.runtime._backend.facts import (
    _DirectoryEntry,
    _FileEditRequest,
    _FileEditResult,
    _FileMetadata,
    _FilesystemError,
    _FileWriteRequest,
    _FileWriteResult,
    _ResolvedPath,
)

if TYPE_CHECKING:
    from cli_agent.runtime._backend.docker.backend import _DockerBackendWorkspace

_FS_SERVER = r"""
import base64
import errno
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

ROOT = "/workspace"


def respond(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def fs_error(path, exc):
    code = getattr(exc, "errno", None)
    if code == errno.ENOENT:
        return {"kind": "not_found", "message": f"No such file or directory: {path}"}
    if code == errno.ENOTDIR:
        return {"kind": "not_a_directory", "message": f"path component is not a directory: {path}"}
    if code == errno.EISDIR:
        return {"kind": "is_directory", "message": f"is a directory: {path}"}
    if code == errno.EACCES:
        return {"kind": "permission_denied", "message": f"permission denied: {path}"}
    if code == errno.EEXIST:
        return {"kind": "already_exists", "message": f"path already exists: {path}"}
    return {"kind": "internal", "message": f"filesystem error for {path}: {exc}"}


def resolve(path):
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(ROOT) / candidate
    return Path(os.path.normpath(os.path.abspath(str(candidate))))


def metadata(info):
    if stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(info.st_mode):
        kind = "directory"
    elif stat.S_ISREG(info.st_mode):
        kind = "file"
    else:
        kind = "other"
    return {
        "kind": kind,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "mode": stat.S_IMODE(info.st_mode),
    }


def atomic_write(target, content):
    try:
        mode = stat.S_IMODE(target.stat().st_mode) if os.path.lexists(target) else None
    except OSError:
        mode = None
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent, prefix=".cli-agent-write-"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), 0o644 if mode is None else mode)
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            try:
                temporary.unlink()
            except OSError:
                pass


def handle(req):
    op = req.get("op")
    try:
        if op == "stat":
            target = resolve(req["path"])
            return {"ok": True, "metadata": metadata(os.stat(target))}
        if op == "list":
            target = resolve(req["path"])
            entries = []
            with os.scandir(target) as iterator:
                for entry in iterator:
                    entries.append(
                        {
                            "name": entry.name,
                            "metadata": metadata(entry.stat(follow_symlinks=False)),
                        }
                    )
            entries.sort(key=lambda entry: entry["name"])
            return {"ok": True, "entries": entries}
        if op == "read":
            target = resolve(req["path"])
            content = target.read_bytes()
            return {
                "ok": True,
                "content": base64.b64encode(content).decode("ascii"),
            }
        if op == "write":
            target = resolve(req["path"])
            if target.is_dir() and not target.is_symlink():
                return {
                    "ok": False,
                    "kind": "is_directory",
                    "message": f"is a directory: {req['path']}",
                }
            target.parent.mkdir(parents=True, exist_ok=True)
            content = base64.b64decode(req["content"])
            atomic_write(target, content)
            return {"ok": True, "bytes_written": len(content)}
        if op == "remove":
            target = resolve(req["path"])
            recursive = bool(req.get("recursive", False))
            if target.is_dir() and not target.is_symlink():
                if not recursive:
                    return {
                        "ok": False,
                        "kind": "is_directory",
                        "message": f"is a directory: {req['path']}",
                    }
                shutil.rmtree(target)
            else:
                os.unlink(target)
            return {"ok": True}
        return {"ok": False, "kind": "internal", "message": f"unsupported op: {op}"}
    except OSError as exc:
        return {"ok": False, **fs_error(req.get("path", ""), exc)}
    except Exception as exc:
        return {
            "ok": False,
            "kind": "internal",
            "message": f"{type(exc).__name__}: {exc}",
        }


for line in sys.stdin:
    if not line.strip():
        continue
    respond(handle(json.loads(line)))
"""


class _DockerWorkspaceFilesystem:
    """Volume-backed Workspace Filesystem running inside the container."""

    def __init__(
        self,
        workspace: _DockerBackendWorkspace,
    ) -> None:
        self._workspace = workspace
        self._root = workspace.root

    def resolve(self, path: str, cwd: str) -> _ResolvedPath:
        """Resolve one container-native path against a Session cwd without I/O."""

        self._workspace._ensure_open()
        target = _resolve_path(self._root, cwd, path)
        return _ResolvedPath(
            path=target,
            within_workspace=_is_within(target, self._root),
        )

    async def stat(self, path: str) -> _FileMetadata:
        self._workspace._ensure_open()
        response = await self._call({"op": "stat", "path": path})
        return _FileMetadata(**response["metadata"])

    async def list(self, path: str) -> tuple[_DirectoryEntry, ...]:
        self._workspace._ensure_open()
        response = await self._call({"op": "list", "path": path})
        return tuple(
            _DirectoryEntry(
                name=entry["name"],
                metadata=_FileMetadata(**entry["metadata"]),
            )
            for entry in response["entries"]
        )

    async def read(self, path: str) -> bytes:
        self._workspace._ensure_open()
        response = await self._call({"op": "read", "path": path})
        return base64.b64decode(response["content"])

    async def write(self, request: _FileWriteRequest) -> _FileWriteResult:
        self._workspace._ensure_open()
        response = await self._call(
            {
                "op": "write",
                "path": request.path,
                "content": base64.b64encode(request.content).decode("ascii"),
            }
        )
        return _FileWriteResult(
            path=request.path,
            bytes_written=response["bytes_written"],
        )

    async def edit(self, request: _FileEditRequest) -> _FileEditResult:
        """Apply one atomic read-modify-write inside the container namespace.

        The exact-text edit logic is shared with the Local Backend; the
        resulting content is committed through one atomic container-side
        replace, so a single edit never leaves a partial file.
        """

        self._workspace._ensure_open()
        content = await self.read(request.path)
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
        self._workspace._ensure_open()
        await self._call({"op": "remove", "path": path, "recursive": recursive})

    async def _call(self, payload: dict[str, object]) -> dict[str, object]:
        """Run one filesystem request in the helper container.

        Args:
            payload (`dict[str, object]`): One JSON-serializable request.

        Returns:
            The structured ``ok`` response dict.

        Raises:
            `_FilesystemError`: For container-reported operation failures,
                an invalid helper response, or a broken helper transport.
        """

        response = await self._workspace._fs_call(payload)
        if not response.get("ok"):
            raise _FilesystemError(
                str(response.get("kind", "internal")),
                str(response.get("message", "container filesystem operation failed")),
            )
        return response


def _resolve_path(root: str, cwd: str, path: str) -> str:
    candidate = path if posixpath.isabs(path) else posixpath.join(cwd, path)
    return posixpath.normpath(candidate)


def _is_within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")
