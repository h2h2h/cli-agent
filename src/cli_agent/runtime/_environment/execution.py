"""Backend-neutral Execution state and observation helpers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone

from cli_agent.runtime._environment.policy import ExecutionDecision
from cli_agent.runtime._environment.routing import _ExecutionRoute
from cli_agent.runtime.model import JSONValue

_TERMINAL_STATUSES = frozenset({"exited", "failed", "killed"})


@dataclass(slots=True)
class _ExecutionRecord:
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
    process: asyncio.subprocess.Process | None = None
    completion_task: asyncio.Task[None] | None = None
    process_ready: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Condition = field(default_factory=asyncio.Condition)
    termination_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


def _snapshot(
    record: _ExecutionRecord,
    *,
    cursor: int,
    limit: int,
) -> dict[str, JSONValue]:
    chunks = list(record.chunks[cursor : cursor + limit])
    next_cursor = cursor + len(chunks)
    return {
        "ok": True,
        "exec_id": record.exec_id,
        "status": record.status,
        "exit_code": record.exit_code,
        "chunks": chunks,
        "next_cursor": next_cursor,
        "is_terminal": record.is_terminal,
        "truncated": record.truncated,
        "available_from": 0,
    }


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


async def _notify_changed(record: _ExecutionRecord) -> None:
    async with record.changed:
        record.changed.notify_all()


async def _wait_until_terminal(
    record: _ExecutionRecord,
    *,
    timeout: float,
) -> None:
    async with record.changed:
        await asyncio.wait_for(
            record.changed.wait_for(lambda: record.is_terminal),
            timeout=timeout,
        )
