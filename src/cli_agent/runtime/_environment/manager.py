"""Session-scoped Execution management and command lifecycles."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._environment.records import (
    ExecutionRecord,
    OutputBuffer,
    _notify_changed,
)
from cli_agent.runtime._environment.router import _ExecutionRoute
from cli_agent.runtime._environment.scheduler import _ExecutionScheduler
from cli_agent.runtime._execution import (
    BackendExecutionError,
    ExecutionOutputSink,
    ExitStatus,
)

if TYPE_CHECKING:
    from cli_agent.runtime._environment.kernel import EnvironmentKernel


class ExecutionManager:
    """Own one Session's admitted Executions and command lifecycles.

    The manager is an organ of its Session Kernel: it reads the Kernel's
    workspace, cwd, environment, and Execution registry through the ``session``
    reference and reports cwd changes through the Kernel's ``_set_cwd``. It
    knows nothing about Backend mechanics, signals, or worker transports.
    """

    def __init__(
        self,
        session: EnvironmentKernel,
        *,
        queue_limit: int,
        parallel_limit: int,
        chunk_limit: int,
        byte_limit: int,
    ) -> None:
        self._session = session
        self._scheduler = _ExecutionScheduler(
            queue_limit,
            parallel_limit,
        )
        self._chunk_limit = chunk_limit
        self._byte_limit = byte_limit

    def admit(
        self,
        request: _ExecutionRequest,
        route: _ExecutionRoute,
    ) -> ExecutionRecord | None:
        """Accept routed work and start whatever the Scheduler claims."""

        admission = self._scheduler.admit(request, route)
        if admission is None:
            return None

        record = admission.state
        self._session._executions[record.exec_id] = record
        for runnable in admission.runnable:
            self._start_execution(runnable)
        return record

    async def wait_for_output(
        self,
        record: ExecutionRecord,
        *,
        cursor: int,
        wait_ms: int,
    ) -> None:
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

    async def terminate(self, record: ExecutionRecord) -> None:
        async with record.termination_lock:
            if record.is_terminal:
                return

            if record.status == "queued" and self._scheduler.cancel_pending(record):
                await _notify_changed(record)
                return

            record.kill_requested = True
            execution = record.handle
            if execution is not None:
                await execution.kill()
            if record.completion_task is not None:
                with suppress(Exception):
                    await record.completion_task

    async def close(self) -> None:
        """Stop promotion, kill queued work, and terminate all Executions."""

        pending = self._scheduler.close()
        for record in pending:
            record.kill_requested = True
            record.status = "killed"
            await _notify_changed(record)
        for record in tuple(self._session._executions.values()):
            await self.terminate(record)
        self._session._executions.clear()

    def _start_execution(self, record: ExecutionRecord) -> None:
        session = self._session
        source = record.route.source
        isolate_context = source.isolated or record.route.parallel_safe
        context = _CommandContext(
            workspace=session._workspace.root,
            cwd=session._cwd,
            environment=dict(session._env) if isolate_context else session._env,
            set_cwd=None if isolate_context else session._set_cwd,
        )
        try:
            execution = source.prepare(
                record.request,
                context,
            )
        except Exception:
            execution = _InlineExecution(_preparation_failed)
        record.handle = execution
        record.completion_task = asyncio.create_task(self._run_execution(record))

    async def _run_execution(self, record: ExecutionRecord) -> None:
        execution = record.handle
        if execution is None:
            raise RuntimeError("running Execution has no ExecutionHandle")
        output = OutputBuffer(
            record,
            chunk_bound=self._chunk_limit,
            byte_bound=self._byte_limit,
        )
        try:
            if record.kill_requested:
                await execution.kill()
            exit_status = await execution.run(output)
        except asyncio.CancelledError:
            with suppress(Exception):
                await execution.kill()
            raise
        except BackendExecutionError as exc:
            record.status = "failed"
            record.exit_code = None
            self._session._emit_diagnostic(
                "execution.backend_failed",
                "execution Backend mechanism failed",
                detail={"exception": repr(exc)},
            )
        except Exception:
            record.status = "killed" if record.kill_requested else "failed"
            record.exit_code = None
        else:
            record.exit_code = exit_status
            if record.kill_requested:
                record.status = "killed"
            elif exit_status == 0:
                record.status = "exited"
            else:
                record.status = "failed"
        for runnable in self._scheduler.complete(record):
            self._start_execution(runnable)
        await _notify_changed(record)


async def _preparation_failed(sink: ExecutionOutputSink) -> ExitStatus:
    del sink
    raise BackendExecutionError("command preparation failed")
