"""Session-scoped Execution supervision and prepared command lifecycle."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING

from cli_agent.runtime._environment.execution_state import (
    _ExecutionState,
    _notify_changed,
    _StateOutput,
)
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.handlers.executions import _InlineExecution
from cli_agent.runtime._environment.routing import _ExecutionRoute
from cli_agent.runtime._environment.scheduler import _ExecutionScheduler
from cli_agent.runtime._execution import (
    BackendExecutionError,
    ExecutionOutputSink,
    ExitStatus,
)

if TYPE_CHECKING:
    from cli_agent.runtime._environment.kernel import EnvironmentKernel


class _ExecutionSupervisor:
    """Own one Session's admitted Executions and command lifecycles.

    The supervisor is an organ of its Session Kernel: it reads the Kernel's
    workspace, cwd, environment, and Execution registry through the ``session``
    reference and reports cwd changes through the Kernel's ``_set_cwd``.
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
    ) -> _ExecutionState | None:
        """Accept routed work and start whatever the Scheduler claims."""

        admission = self._scheduler.admit(request, route)
        if admission is None:
            return None

        state = admission.state
        self._session._executions[state.exec_id] = state
        for runnable in admission.runnable:
            self._start_execution(runnable)
        return state

    async def wait_for_output(
        self,
        state: _ExecutionState,
        *,
        cursor: int,
        wait_ms: int,
    ) -> None:
        if wait_ms <= 0 or cursor < len(state.chunks) or state.is_terminal:
            return
        with suppress(asyncio.TimeoutError):
            async with state.changed:
                await asyncio.wait_for(
                    state.changed.wait_for(
                        lambda: cursor < len(state.chunks) or state.is_terminal
                    ),
                    timeout=wait_ms / 1000,
                )

    async def terminate(self, state: _ExecutionState) -> None:
        async with state.termination_lock:
            if state.is_terminal:
                return

            if state.status == "queued" and self._scheduler.cancel_pending(state):
                await _notify_changed(state)
                return

            state.kill_requested = True
            execution = state.handle
            if execution is None:
                raise RuntimeError("running Execution has no ExecutionHandle")
            await execution.kill()
            if state.completion_task is not None:
                with suppress(Exception):
                    await state.completion_task

    async def close(self) -> None:
        """Stop promotion, kill queued work, and terminate all Executions."""

        pending = self._scheduler.close()
        for state in pending:
            state.kill_requested = True
            state.status = "killed"
            await _notify_changed(state)
        for state in tuple(self._session._executions.values()):
            await self.terminate(state)
        self._session._executions.clear()

    def _start_execution(self, state: _ExecutionState) -> None:
        session = self._session
        isolate_context = state.route.command.isolated or state.route.parallel_safe
        context = _CommandContext(
            workspace=session._workspace,
            cwd=session._cwd,
            environment=dict(session._env) if isolate_context else session._env,
            set_cwd=None if isolate_context else session._set_cwd,
        )
        try:
            execution = state.route.command.prepare(
                state.request,
                context,
            )
        except Exception:
            execution = _InlineExecution(_preparation_failed)
        state.handle = execution
        state.completion_task = asyncio.create_task(self._run_execution(state))

    async def _run_execution(self, state: _ExecutionState) -> None:
        execution = state.handle
        if execution is None:
            raise RuntimeError("running Execution has no ExecutionHandle")
        output = _StateOutput(
            state,
            chunk_bound=self._chunk_limit,
            byte_bound=self._byte_limit,
        )
        try:
            exit_status = await execution.run(output)
        except asyncio.CancelledError:
            with suppress(Exception):
                await execution.kill()
            raise
        except BackendExecutionError:
            state.status = "failed"
            state.exit_code = None
        except Exception:
            state.status = "killed" if state.kill_requested else "failed"
            state.exit_code = None
        else:
            state.exit_code = exit_status
            if state.kill_requested:
                state.status = "killed"
            elif exit_status == 0:
                state.status = "exited"
            else:
                state.status = "failed"
        for runnable in self._scheduler.complete(state):
            self._start_execution(runnable)
        await _notify_changed(state)


async def _preparation_failed(sink: ExecutionOutputSink) -> ExitStatus:
    del sink
    raise BackendExecutionError("command preparation failed")
