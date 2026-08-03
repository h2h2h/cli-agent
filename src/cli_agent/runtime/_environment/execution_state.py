"""Backend-neutral live Execution state and observation helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._environment.handlers.base import _PreparedExecution
from cli_agent.runtime._environment.routing import _ExecutionRoute
from cli_agent.runtime.model import JSONValue

_TERMINAL_STATUSES = frozenset({"exited", "failed", "killed"})


@dataclass(slots=True)
class _ExecutionState:
    exec_id: str
    command: ShellParseResult
    route: _ExecutionRoute
    status: str = "queued"
    submission_sequence: int | None = None
    exit_code: int | None = None
    chunks: list[dict[str, JSONValue]] = field(default_factory=list)
    retained_bytes: int = 0
    truncated: bool = False
    kill_requested: bool = False
    prepared_execution: _PreparedExecution | None = None
    completion_task: asyncio.Task[None] | None = None
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


class _StateOutput:
    """Bound and publish command output for one live Execution State."""

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
