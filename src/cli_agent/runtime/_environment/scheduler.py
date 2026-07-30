"""Ordered per-Session admission with trusted parallel-safe batches."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from cli_agent.runtime._environment.execution import _ExecutionState
from cli_agent.runtime._environment.policy import ExecutionDecision
from cli_agent.runtime._environment.routing import (
    _ExecutionRoute,
    _SchedulingClass,
)

_DEFAULT_QUEUE_LIMIT = 32
_DEFAULT_PARALLEL_LIMIT = 4


def _validate_queue_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("pending_execution_capacity must be an integer >= 1")
    return value


def _validate_parallel_limit(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("parallel_execution_capacity must be an integer >= 1")
    return value


@dataclass(frozen=True, slots=True)
class _SchedulerAdmission:
    state: _ExecutionState
    runnable: tuple[_ExecutionState, ...]


class _ExecutionScheduler:
    """Preserve AEP order while batching consecutive parallel-safe commands."""

    def __init__(
        self,
        queue_limit: int,
        parallel_limit: int = _DEFAULT_PARALLEL_LIMIT,
    ) -> None:
        self._queue_limit = _validate_queue_limit(queue_limit)
        self._parallel_limit = _validate_parallel_limit(parallel_limit)
        self._next_submission_sequence = 0
        self._pending: list[_ExecutionState] = []
        self._running: dict[str, _ExecutionState] = {}
        self._closed = False

    def admit(
        self,
        decision: ExecutionDecision,
        route: _ExecutionRoute,
    ) -> _SchedulerAdmission | None:
        """Accept allowed work when its required pending capacity is available."""

        if self._closed:
            raise RuntimeError("Execution Scheduler is closed")
        if not decision.allowed:
            raise RuntimeError("Execution Scheduler received a denied decision")
        if not self._can_start_immediately(route) and (
            len(self._pending) >= self._queue_limit
        ):
            return None

        state = _ExecutionState(
            exec_id=uuid4().hex,
            decision=decision,
            route=route,
            submission_sequence=self._next_submission_sequence,
        )
        self._next_submission_sequence += 1
        self._pending.append(state)
        return _SchedulerAdmission(
            state=state,
            runnable=self._claim_runnable(),
        )

    def complete(self, state: _ExecutionState) -> tuple[_ExecutionState, ...]:
        """Release one running Execution and promote ordered follow-up work."""

        self._running.pop(state.exec_id, None)
        if self._closed:
            return ()
        return self._claim_runnable()

    def cancel_pending(self, state: _ExecutionState) -> bool:
        """Atomically terminate one queued Execution before it is claimed."""

        for index, pending in enumerate(self._pending):
            if pending is state:
                self._pending.pop(index)
                state.kill_requested = True
                state.status = "killed"
                return True
        return False

    def close(self) -> tuple[_ExecutionState, ...]:
        """Stop promotion and release all queued Executions."""

        if self._closed:
            return ()
        self._closed = True
        pending = tuple(self._pending)
        self._pending.clear()
        return pending

    def _can_start_immediately(self, route: _ExecutionRoute) -> bool:
        if self._pending:
            return False
        if not self._running:
            return True
        if route.scheduling is _SchedulingClass.SERIAL:
            return False
        if len(self._running) >= self._parallel_limit:
            return False
        return all(
            running.route.scheduling is _SchedulingClass.PARALLEL_SAFE
            for running in self._running.values()
        )

    def _claim_runnable(self) -> tuple[_ExecutionState, ...]:
        if not self._pending:
            return ()
        if any(
            state.route.scheduling is _SchedulingClass.SERIAL
            for state in self._running.values()
        ):
            return ()

        head = self._pending[0]
        if head.route.scheduling is _SchedulingClass.SERIAL:
            if self._running:
                return ()
            return (self._claim_head(),)

        claimed: list[_ExecutionState] = []
        while (
            self._pending
            and len(self._running) < self._parallel_limit
            and self._pending[0].route.scheduling
            is _SchedulingClass.PARALLEL_SAFE
        ):
            claimed.append(self._claim_head())
        return tuple(claimed)

    def _claim_head(self) -> _ExecutionState:
        state = self._pending.pop(0)
        self._running[state.exec_id] = state
        state.status = "running"
        return state
