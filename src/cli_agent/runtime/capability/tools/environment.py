"""Workspace-private Python dependency environment for Tool workers."""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import stat
import tempfile
import venv
import weakref
from dataclasses import dataclass
from pathlib import Path

from cli_agent.runtime.capability.view import _CapabilityView

_STATE_DIRECTORY = ".tool-environment"
_VENV_DIRECTORY = ".venv"
_EFFECTIVE_REQUIREMENTS = "effective-requirements.txt"
_REQUIREMENTS_DIGEST = "requirements.sha256"
_RECONCILE_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[Path, asyncio.Lock],
] = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class _ToolEnvironment:
    """One reconciled Tool environment or a fail-soft unavailable state."""

    root: Path
    python: Path | None
    error: str | None

    @property
    def available(self) -> bool:
        return self.python is not None and self.error is None

    @classmethod
    async def reconcile(
        cls,
        capability_view: _CapabilityView,
    ) -> _ToolEnvironment:
        root = capability_view.root / _STATE_DIRECTORY
        loop = asyncio.get_running_loop()
        locks = _RECONCILE_LOCKS.setdefault(loop, {})
        lock = locks.setdefault(root, asyncio.Lock())
        async with lock:
            return await cls._reconcile_locked(capability_view, root)

    @classmethod
    async def _reconcile_locked(
        cls,
        capability_view: _CapabilityView,
        root: Path,
    ) -> _ToolEnvironment:
        try:
            _ensure_real_directory(root)
            venv_directory = root / _VENV_DIRECTORY
            created = not venv_directory.exists()
            if created:
                await asyncio.to_thread(_create_venv, venv_directory)
            else:
                _ensure_real_directory(venv_directory)

            python = _venv_python(venv_directory)
            requirements = capability_view.root / "tools" / "requirements.txt"
            content = requirements.read_bytes() if requirements.is_file() else b""
            digest = hashlib.sha256(content).hexdigest()
            effective = root / _EFFECTIVE_REQUIREMENTS
            marker = root / _REQUIREMENTS_DIGEST
            _atomic_write(effective, content)
            previous = (
                marker.read_text(encoding="ascii").strip()
                if marker.is_file()
                else None
            )
            if previous != digest:
                if content or not created:
                    await _sync_requirements(
                        python=python,
                        requirements=effective,
                        working_directory=requirements.parent,
                    )
                _atomic_write(marker, (digest + "\n").encode("ascii"))
            return cls(root=root, python=python, error=None)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return cls(
                root=root,
                python=None,
                error=f"Tool environment is unavailable: {exc}",
            )


def _create_venv(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Tool venv path must not be a symbolic link: {path}")
    venv.EnvBuilder(
        system_site_packages=False,
        clear=False,
        symlinks=os.name != "nt",
        with_pip=False,
    ).create(path)


def _venv_python(venv_directory: Path) -> Path:
    candidates = (
        venv_directory / "Scripts" / "python.exe",
        venv_directory / "bin" / "python",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ValueError(f"Tool venv Python was not created: {venv_directory}")


async def _sync_requirements(
    *,
    python: Path,
    requirements: Path,
    working_directory: Path,
) -> None:
    argv = [
        "uv",
        "pip",
        "sync",
        "--python",
        str(python),
        "--no-progress",
        "--allow-empty-requirements",
    ]
    argv.append(str(requirements))
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=working_directory,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
    except FileNotFoundError as exc:
        raise RuntimeError("uv is required to synchronize Tool dependencies") from exc

    try:
        stdout, stderr = await process.communicate()
    except asyncio.CancelledError:
        if process.returncode is None:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.kill()
                await process.wait()
        raise
    if process.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
        if len(detail) > 4_000:
            detail = detail[-4_000:]
        raise RuntimeError(
            "dependency synchronization failed"
            + (f": {detail}" if detail else "")
        )


def _ensure_real_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    mode = path.lstat().st_mode
    if not stat.S_ISDIR(mode):
        raise ValueError(f"Tool environment path must be a real directory: {path}")


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
