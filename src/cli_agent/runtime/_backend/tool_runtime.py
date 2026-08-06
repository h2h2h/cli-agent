"""Local Backend Tool Runtime: Workspace-private venv and materialized worker.

The Local Backend owns the Tool worker execution environment: it reconciles
a Workspace-private virtual environment from the effective Tool requirements,
materializes the Runtime-owned worker into that environment, and exposes the
Host paths that ``prepare_tool`` needs to spawn one fresh worker. All of this
is Local-only mechanical detail; the generic Backend contract only sees the
backend-neutral ``_ToolRuntimeStatus`` returned by reconcile.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import signal
import venv
import weakref
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from cli_agent.runtime._backend.facts import _ToolRuntimeStatus
from cli_agent.runtime._capability.workspace import (
    _atomic_write,
    _ensure_real_directory,
)

_EFFECTIVE_REQUIREMENTS = "effective-requirements.txt"
_REQUIREMENTS_DIGEST = "requirements.sha256"
_RUNTIME_BASE_REQUIREMENTS = "mcp"
_WORKER_FILENAME = "worker.py"
_RECONCILE_LOCKS: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[Path, asyncio.Lock],
] = weakref.WeakKeyDictionary()


@dataclass(frozen=True, slots=True)
class _LocalToolRuntime:
    """One reconciled Local Tool Runtime or a fail-soft unavailable state."""

    root: Path
    python: Path | None
    worker: Path | None
    tools_directory: Path | None
    error: str | None

    @property
    def available(self) -> bool:
        return (
            self.python is not None
            and self.worker is not None
            and self.tools_directory is not None
            and self.error is None
        )

    @property
    def status(self) -> _ToolRuntimeStatus:
        return _ToolRuntimeStatus(available=self.available, error=self.error)

    @classmethod
    async def reconcile(cls, view_root: Path) -> _LocalToolRuntime:
        """Reconcile one Tool Runtime under a materialized Capability View.

        Args:
            view_root (`Path`):
                The Local Bound Capability View root; the venv and the
                materialized worker live under ``view_root/.tool-environment``.

        Returns:
            The reconciled Local Tool Runtime; dependency failures produce an
            unavailable Runtime instead of raising.
        """

        root = view_root / ".tool-environment"
        loop = asyncio.get_running_loop()
        locks = _RECONCILE_LOCKS.setdefault(loop, {})
        lock = locks.setdefault(root, asyncio.Lock())
        async with lock:
            return await cls._reconcile_locked(view_root, root)

    @classmethod
    async def _reconcile_locked(
        cls,
        view_root: Path,
        root: Path,
    ) -> _LocalToolRuntime:
        try:
            _ensure_real_directory(root, label="Tool environment path")
            venv_directory = root / ".venv"
            created = not venv_directory.exists()
            if created:
                await asyncio.to_thread(_create_venv, venv_directory)
            else:
                _ensure_real_directory(
                    venv_directory,
                    label="Tool venv path",
                )

            python = _venv_python(venv_directory)
            requirements = view_root / "tools" / "requirements.txt"
            content = requirements.read_bytes() if requirements.is_file() else b""
            effective_content = _effective_requirements(content)
            digest = hashlib.sha256(effective_content).hexdigest()
            effective = root / _EFFECTIVE_REQUIREMENTS
            marker = root / _REQUIREMENTS_DIGEST
            _atomic_write(effective, effective_content)
            previous = (
                marker.read_text(encoding="ascii").strip() if marker.is_file() else None
            )
            if previous != digest:
                if effective_content or not created:
                    await _sync_requirements(
                        python=python,
                        requirements=effective,
                        working_directory=requirements.parent,
                    )
                _atomic_write(marker, (digest + "\n").encode("ascii"))

            worker = root / _WORKER_FILENAME
            template = (
                files("cli_agent.runtime._backend")
                .joinpath(_WORKER_FILENAME)
                .read_bytes()
            )
            _atomic_write(worker, template)
            return cls(
                root=root,
                python=python,
                worker=worker,
                tools_directory=view_root / "tools",
                error=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return cls(
                root=root,
                python=None,
                worker=None,
                tools_directory=None,
                error=f"Tool environment is unavailable: {exc}",
            )


def _effective_requirements(content: bytes) -> bytes:
    """Combine user requirements with the Runtime-owned base dependency.

    The base dependency is appended unless the user already declares it, so a
    M13 self-connecting stub can import ``mcp`` in the worker venv. M14 removes
    the worker's need for ``mcp`` (stubs switch to the IPC shim).
    """

    lines = content.decode("utf-8").splitlines()
    if any(line.strip() == _RUNTIME_BASE_REQUIREMENTS for line in lines):
        effective = lines
    else:
        effective = [*lines, _RUNTIME_BASE_REQUIREMENTS]
    return ("\n".join(effective) + "\n").encode("utf-8")


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
    lockfile = requirements.parent / ".requirements.lock"
    await _run_uv(
        [
            "pip",
            "compile",
            "--python",
            str(python),
            "--no-progress",
            "--output-file",
            str(lockfile),
            str(requirements),
        ],
        working_directory=working_directory,
    )
    await _run_uv(
        [
            "pip",
            "sync",
            "--python",
            str(python),
            "--no-progress",
            "--allow-empty-requirements",
            str(lockfile),
        ],
        working_directory=working_directory,
    )


async def _run_uv(
    argv: list[str],
    *,
    working_directory: Path,
) -> None:
    try:
        process = await asyncio.create_subprocess_exec(
            "uv",
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
            "dependency synchronization failed" + (f": {detail}" if detail else "")
        )
