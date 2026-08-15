"""Local Tool Runtime mechanics: Workspace-private venv and dependencies.

The CapabilityDeployment plane owns what gets deployed; this module owns
the Local mechanical detail of keeping the private virtual environment in
sync with the effective Tool requirements. The materialized worker and the
effective requirements file are published by the deployment through the
Workspace filesystem; ``ensure`` validates the venv, runs the digest-gated
dependency synchronization, and returns the Host paths the Local
ToolExecutor needs to spawn one fresh worker.
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
    """One ensured Local Tool Runtime or a fail-soft unavailable state."""

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


def worker_template() -> bytes:
    """Return the packaged Runtime-owned worker template bytes."""

    return (
        files("cli_agent.runtime._backend.local")
        .joinpath(_WORKER_FILENAME)
        .read_bytes()
    )


async def ensure_tool_runtime(
    root: Path,
    *,
    tools_directory: Path,
    effective_content: bytes,
) -> _LocalToolRuntime:
    """Ensure the venv and digest-gated dependencies under one Tool Runtime root.

    Args:
        root (`Path`):
            The Local Tool Runtime root (``<view root>/.tool-environment``).
        tools_directory (`Path`):
            The effective Tools directory used as the sync working directory.
        effective_content (`bytes`):
            The effective requirements content published by the deployment;
            its digest gates the dependency synchronization.

    Returns:
        The ensured Local Tool Runtime; dependency failures produce an
        unavailable Runtime instead of raising.
    """

    loop = asyncio.get_running_loop()
    locks = _RECONCILE_LOCKS.setdefault(loop, {})
    lock = locks.setdefault(root, asyncio.Lock())
    async with lock:
        return await _ensure_locked(
            root,
            tools_directory=tools_directory,
            effective_content=effective_content,
        )


async def _ensure_locked(
    root: Path,
    *,
    tools_directory: Path,
    effective_content: bytes,
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
        digest = hashlib.sha256(effective_content).hexdigest()
        marker = root / _REQUIREMENTS_DIGEST
        previous = (
            marker.read_text(encoding="ascii").strip() if marker.is_file() else None
        )
        if previous != digest:
            if effective_content or not created:
                await _sync_requirements(
                    python=python,
                    requirements=root / _EFFECTIVE_REQUIREMENTS,
                    working_directory=tools_directory,
                )
            _atomic_write(marker, (digest + "\n").encode("ascii"))

        return _LocalToolRuntime(
            root=root,
            python=python,
            worker=root / _WORKER_FILENAME,
            tools_directory=tools_directory,
            error=None,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _LocalToolRuntime(
            root=root,
            python=None,
            worker=None,
            tools_directory=None,
            error=f"Tool environment is unavailable: {exc}",
        )


def effective_requirements(
    content: bytes,
    *,
    include_mcp: bool = True,
) -> bytes:
    """Combine user requirements with the Runtime-owned base dependency.

    The base dependency is appended when MCP bindings are deployed unless the
    user already declares it. Workspaces without MCP servers do not install an
    otherwise unused network dependency.
    """

    lines = content.decode("utf-8").splitlines()
    if not include_mcp:
        effective = lines
    elif any(line.strip() == _RUNTIME_BASE_REQUIREMENTS for line in lines):
        effective = lines
    else:
        effective = [*lines, _RUNTIME_BASE_REQUIREMENTS]
    if not effective:
        return b""
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
