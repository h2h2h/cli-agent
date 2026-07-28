"""Session-scoped Execution lifecycle supervision."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field

from cli_agent.runtime._environment.drivers.shell import _ShellDriver
from cli_agent.runtime._environment.execution import (
    _ExecutionRecord,
    _notify_changed,
)
from cli_agent.runtime._environment.policy import ExecutionDecision
from cli_agent.runtime._environment.routing import _DriverKind, _ExecutionRoute
from cli_agent.runtime._environment.scheduler import _ExecutionScheduler


@dataclass(slots=True)
class _EnvironmentSession:
    """Supervise the Executions owned by one Environment Session."""

    scheduler: _ExecutionScheduler
    shell_driver: _ShellDriver
    executions: dict[str, _ExecutionRecord] = field(default_factory=dict)
    closing: bool = False

    def admit(
        self,
        decision: ExecutionDecision,
        route: _ExecutionRoute,
    ) -> _ExecutionRecord | None:
        """Admit one allowed decision and start any newly runnable work."""

        admission = self.scheduler.admit(decision, route)
        if admission is None:
            return None

        record = admission.record
        self.executions[record.exec_id] = record
        for runnable in admission.runnable:
            self._start_execution(runnable)
        return record

    async def wait_for_output(
        self,
        record: _ExecutionRecord,
        *,
        cursor: int,
        wait_ms: int,
    ) -> None:
        """Wait until output at the requested Cursor or a terminal state exists."""

        if wait_ms <= 0 or cursor < len(record.chunks) or record.is_terminal:
            return
        with suppress(asyncio.TimeoutError):
            async with record.changed:
                await asyncio.wait_for(
                    record.changed.wait_for(
                        lambda: cursor < len(record.chunks) or record.is_terminal
                    ),
                    timeout=wait_ms / 1000,
                )

    def begin_close(self) -> tuple[_ExecutionRecord, ...]:
        """Stop promotion and terminally release queued Executions."""

        if self.closing:
            return ()
        self.closing = True
        pending = self.scheduler.close()
        for record in pending:
            record.kill_requested = True
            record.status = "killed"
            record.process_ready.set()
        return pending

    async def release(
        self,
        pending: tuple[_ExecutionRecord, ...],
    ) -> None:
        """Notify queued work, terminate running work, and forget all handles."""

        for record in pending:
            await _notify_changed(record)
        for record in tuple(self.executions.values()):
            await self.terminate(record)
        self.executions.clear()

    async def terminate(self, record: _ExecutionRecord) -> None:
        """Terminate one queued or running Execution idempotently."""

        async with record.termination_lock:
            if record.is_terminal:
                return

            if record.status == "queued" and self.scheduler.cancel_pending(record):
                record.process_ready.set()
                await _notify_changed(record)
                return

            if record.route.driver is not _DriverKind.SHELL:
                raise RuntimeError("unsupported Execution driver")
            await self.shell_driver.terminate(record)

    def _start_execution(self, record: _ExecutionRecord) -> None:
        if record.route.driver is not _DriverKind.SHELL:
            raise RuntimeError("unsupported Execution driver")
        record.completion_task = asyncio.create_task(self._run_execution(record))

    async def _run_execution(self, record: _ExecutionRecord) -> None:
        try:
            await self.shell_driver.run(record)
        finally:
            for runnable in self.scheduler.complete(record):
                self._start_execution(runnable)
            await _notify_changed(record)
