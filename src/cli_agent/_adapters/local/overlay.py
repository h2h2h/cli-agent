"""Local CapabilityOverlay over file-level copy-up and whiteout mechanics."""

from __future__ import annotations

from pathlib import Path

from cli_agent._adapters.local.view import _LocalCapabilityView
from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    ExecutionHandle,
    ExecutionOutputSink,
    ExitStatus,
)


class _LocalCapabilityOverlay:
    """Own the materialized Local view and mutation choreography."""

    def __init__(self, view: _LocalCapabilityView) -> None:
        self.view = view

    @classmethod
    def materialize(
        cls,
        *,
        state_root: Path,
        repertoire: Path,
    ) -> _LocalCapabilityOverlay:
        return cls(_LocalCapabilityView.materialize(state_root, repertoire))

    def wrap_file(self, path: str, execution: ExecutionHandle) -> ExecutionHandle:
        return _LocalFileOverlayExecution(self.view, Path(path), execution)

    def wrap_shell(
        self,
        command: ShellParseResult,
        cwd: str,
        execution: ExecutionHandle,
    ) -> ExecutionHandle:
        return _LocalShellOverlayExecution(
            self.view,
            command,
            Path(cwd),
            execution,
        )

    async def close(self) -> None:
        self.view.close()


class _LocalCapabilityOverlayFactory:
    async def create(self, workspace: object) -> _LocalCapabilityOverlay:
        root = Path(str(getattr(workspace, "root")))
        volume = str(getattr(workspace, "deployment_volume"))
        repertoire = getattr(workspace, "repertoire")
        if not isinstance(repertoire, Path):
            raise ValueError("Local overlay requires a Host Repertoire")
        return _LocalCapabilityOverlay.materialize(
            state_root=root / volume,
            repertoire=repertoire,
        )


class _LocalFileOverlayExecution:
    def __init__(
        self,
        view: _LocalCapabilityView,
        path: Path,
        inner: ExecutionHandle,
    ) -> None:
        self._view = view
        self._path = path
        self._inner = inner
        self._run_started = False
        self._kill_requested = False

    async def run(self, sink: ExecutionOutputSink) -> ExitStatus:
        if self._run_started:
            raise RuntimeError("ExecutionHandle.run called more than once")
        self._run_started = True
        if self._kill_requested:
            return ExitStatus(_KILLED_BEFORE_START)
        self._view.prepare_path(self._path)
        return await self._inner.run(sink)

    async def kill(self) -> None:
        self._kill_requested = True
        await self._inner.kill()


class _LocalShellOverlayExecution:
    def __init__(
        self,
        view: _LocalCapabilityView,
        command: ShellParseResult,
        cwd: Path,
        inner: ExecutionHandle,
    ) -> None:
        self._view = view
        self._command = command
        self._cwd = cwd
        self._inner = inner
        self._run_started = False
        self._kill_requested = False

    async def run(self, sink: ExecutionOutputSink) -> ExitStatus:
        if self._run_started:
            raise RuntimeError("ExecutionHandle.run called more than once")
        self._run_started = True
        async with self._view.prepare_shell(
            self._command,
            self._cwd,
            cancelled=lambda: self._kill_requested,
        ) as prepared:
            if not prepared:
                return ExitStatus(_KILLED_BEFORE_START)
            return await self._inner.run(sink)

    async def kill(self) -> None:
        self._kill_requested = True
        await self._inner.kill()
