"""Session-scoped Execution admission and lane scheduling."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from cli_agent.runtime._environment.execution import _ExecutionRecord
from cli_agent.runtime._environment.policy import ExecutionDecision
from cli_agent.runtime._environment.routing import _ExecutionLane, _ExecutionRoute

_DEFAULT_PENDING_EXECUTION_CAPACITY = 32


@dataclass(frozen=True, slots=True)
class _SchedulerAdmission:
    record: _ExecutionRecord
    runnable: tuple[_ExecutionRecord, ...]


class _ExecutionScheduler:
    """Assign admitted Executions to Runtime-trusted Driver lanes."""

    def __init__(self, pending_capacity: int) -> None:
        self._pending_capacity = _validate_pending_execution_capacity(pending_capacity)
        self._next_submission_sequence = 0
        self._pending: list[_ExecutionRecord] = []
        self._running: dict[_ExecutionLane, set[str]] = {
            _ExecutionLane.SHELL: set(),
        }
        self._lane_capacities = {
            _ExecutionLane.SHELL: 1,
        }
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
        lane = route.lane
        if (
            len(self._running[lane]) >= self._lane_capacities[lane]
            and len(self._pending) >= self._pending_capacity
        ):
            return None

        record = _ExecutionRecord(
            exec_id=uuid4().hex,
            decision=decision,
            route=route,
            submission_sequence=self._next_submission_sequence,
        )
        self._next_submission_sequence += 1
        self._pending.append(record)
        return _SchedulerAdmission(
            record=record,
            runnable=self._claim_runnable(),
        )

    def complete(self, record: _ExecutionRecord) -> tuple[_ExecutionRecord, ...]:
        """Release one running lane occupant and select follow-up work."""

        self._running[record.route.lane].discard(record.exec_id)
        if self._closed:
            return ()
        return self._claim_runnable()

    def cancel_pending(self, record: _ExecutionRecord) -> bool:
        """Atomically terminate one queued Execution before lane claim."""

        for index, pending in enumerate(self._pending):
            if pending is record:
                self._pending.pop(index)
                record.kill_requested = True
                record.status = "killed"
                return True
        return False

    def close(self) -> tuple[_ExecutionRecord, ...]:
        """Stop promotion and release all queued Executions."""

        if self._closed:
            return ()
        self._closed = True
        pending = tuple(self._pending)
        self._pending.clear()
        return pending

    def _claim_runnable(self) -> tuple[_ExecutionRecord, ...]:
        claimed: list[_ExecutionRecord] = []
        index = 0
        while index < len(self._pending):
            record = self._pending[index]
            running = self._running[record.route.lane]
            if len(running) >= self._lane_capacities[record.route.lane]:
                index += 1
                continue

            self._pending.pop(index)
            running.add(record.exec_id)
            record.status = "running"
            claimed.append(record)
        return tuple(claimed)


def _validate_pending_execution_capacity(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("pending_execution_capacity must be an integer >= 1")
    return value
