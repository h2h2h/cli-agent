"""Backend-neutral live Execution state and observation helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from cli_agent.runtime._environment.drivers.base import _DriverExecution
from cli_agent.runtime._environment.policy import ExecutionDecision
from cli_agent.runtime._environment.routing import _ExecutionRoute
from cli_agent.runtime.model import JSONValue

_TERMINAL_STATUSES = frozenset({"exited", "failed", "killed"})


@dataclass(slots=True)
class _ExecutionState:
    exec_id: str
    decision: ExecutionDecision
    route: _ExecutionRoute
    status: str = "queued"
    submission_sequence: int | None = None
    exit_code: int | None = None
    chunks: list[dict[str, JSONValue]] = field(default_factory=list)
    retained_bytes: int = 0
    truncated: bool = False
    kill_requested: bool = False
    driver_execution: _DriverExecution | None = None
    completion_task: asyncio.Task[None] | None = None
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


def _snapshot(
    state: _ExecutionState,
    *,
    cursor: int,
    limit: int,
) -> dict[str, JSONValue]:
    chunks = list(state.chunks[cursor : cursor + limit])
    next_cursor = cursor + len(chunks)
    return {
        "ok": True,
        "exec_id": state.exec_id,
        "status": state.status,
        "exit_code": state.exit_code,
        "chunks": chunks,
        "next_cursor": next_cursor,
        "is_terminal": state.is_terminal,
        "truncated": state.truncated,
        "available_from": 0,
    }


class _StateOutput:
    """Bound and publish Driver output for one live Execution State."""

    def __init__(
        self,
        state: _ExecutionState,
        *,
        chunk_bound: int,
        byte_bound: int,
    ) -> None:
        self._state = state
        self._chunk_bound = chunk_bound
        self._byte_bound = byte_bound

    async def write(
        self,
        stream: Literal["stdout", "stderr"],
        data: bytes,
    ) -> None:
        state = self._state
        if (
            len(state.chunks) >= self._chunk_bound
            or state.retained_bytes + len(data) > self._byte_bound
        ):
            state.truncated = True
            await _notify_changed(state)
            return

        state.chunks.append(
            {
                "cursor": len(state.chunks),
                "stream": stream,
                "text": data.decode("utf-8", errors="replace"),
                "timestamp": _timestamp(),
            }
        )
        state.retained_bytes += len(data)
        await _notify_changed(state)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _notify_changed(state: _ExecutionState) -> None:
    async with state.changed:
        state.changed.notify_all()
