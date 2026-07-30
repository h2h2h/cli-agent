"""Ordered per-Session admission with trusted parallel-safe batches."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from cli_agent.runtime._environment.execution import _ExecutionState
from cli_agent.runtime._environment.policy import ExecutionDecision
from cli_agent.runtime._environment.routing import (
    _ExecutionLane,
    _ExecutionRoute,
    _SchedulingClass,
)

_DEFAULT_QUEUE_LIMIT = 32
_DEFAULT_PARALLEL_LIMIT = 4


@dataclass(frozen=True, slots=True)
class _SchedulerAdmission:
    state: _ExecutionState
    runnable: tuple[_ExecutionState, ...]


class _ExecutionScheduler:
    """Preserve AEP order while batching consecutive parallel-safe commands."""

    def __init__(
        self,
        queue_limit: int = _DEFAULT_QUEUE_LIMIT,
        parallel_limit: int = _DEFAULT_PARALLEL_LIMIT,
        tool_parallel_limit: int = _DEFAULT_PARALLEL_LIMIT,
    ) -> None:
        self._queue_limit = queue_limit
        self._parallel_limit = parallel_limit
        self._tool_parallel_limit = tool_parallel_limit
        self._next_submission_sequence = 0
        self._pending: list[_ExecutionState] = []
        self._running: dict[str, _ExecutionState] = {}
        self._closed = False

    def admit(
        self,
        decision: ExecutionDecision,
        route: _ExecutionRoute,
    ) -> _SchedulerAdmission | None:
        """Accept authorized work when pending capacity is available."""

        if self._closed:
            raise RuntimeError("Execution Scheduler is closed")
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
        if any(pending.route.lane is route.lane for pending in self._pending):
            return False
        lane_running = tuple(
            state
            for state in self._running.values()
            if state.route.lane is route.lane
        )
        if not lane_running:
            return True
        if route.scheduling is _SchedulingClass.SERIAL:
            return False
        if len(lane_running) >= self._lane_limit(route.lane):
            return False
        return all(
            state.route.scheduling is _SchedulingClass.PARALLEL_SAFE
            for state in lane_running
        )

    def _claim_runnable(self) -> tuple[_ExecutionState, ...]:
        if not self._pending:
            return ()

        claimed: list[_ExecutionState] = []
        for lane in _ExecutionLane:
            claimed.extend(self._claim_lane(lane))
        return tuple(claimed)

    def _claim_lane(self, lane: _ExecutionLane) -> tuple[_ExecutionState, ...]:
        running = [
            state for state in self._running.values() if state.route.lane is lane
        ]
        if any(
            state.route.scheduling is _SchedulingClass.SERIAL for state in running
        ):
            return ()
        pending = [state for state in self._pending if state.route.lane is lane]
        if not pending:
            return ()

        head = pending[0]
        if head.route.scheduling is _SchedulingClass.SERIAL:
            if running:
                return ()
            return (self._claim(head),)

        claimed: list[_ExecutionState] = []
        capacity = self._lane_limit(lane) - len(running)
        for state in pending:
            if capacity <= 0:
                break
            if state.route.scheduling is _SchedulingClass.SERIAL:
                break
            claimed.append(self._claim(state))
            capacity -= 1
        return tuple(claimed)

    def _claim(self, state: _ExecutionState) -> _ExecutionState:
        self._pending.remove(state)
        self._running[state.exec_id] = state
        state.status = "running"
        return state

    def _lane_limit(self, lane: _ExecutionLane) -> int:
        return (
            self._tool_parallel_limit
            if lane is _ExecutionLane.TOOL
            else self._parallel_limit
        )
