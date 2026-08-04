"""Schema-aware reduction of stale Execution snapshot Tool Results."""

from __future__ import annotations

from typing import Literal

from cli_agent.runtime.model import JSONValue, ToolCall, ToolResult

SNIP_HEAD_CHUNKS = 6
SNIP_TAIL_CHUNKS = 4
SNIP_CHUNK_MAX_CHARS = 4_000

ReductionState = Literal["raw", "snipped", "pruned"]


class _ToolResultReducer:
    """Reduce recognized Execution snapshots without calling any model."""

    def __init__(self, excluded_tools: frozenset[str] = frozenset()) -> None:
        self._excluded_tools = excluded_tools

    def state_of(self, result: ToolResult) -> ReductionState:
        """Return the monotonic compaction state of one Tool Result."""

        output = result.output
        if isinstance(output, dict):
            reclaimed = output.get("reclaimed")
            if isinstance(reclaimed, dict):
                state = reclaimed.get("state")
                if state == "snipped":
                    return "snipped"
                if state == "pruned":
                    return "pruned"
        return "raw"

    def can_reduce(self, call: ToolCall, result: ToolResult) -> bool:
        """Return whether one success Tool Result is a reduction candidate."""

        if result.error is not None:
            return False
        if call.name in self._excluded_tools:
            return False
        if not _is_execution_snapshot(result.output):
            return False
        return self.state_of(result) != "pruned"

    def snip(self, call: ToolCall, result: ToolResult) -> ToolResult:
        """Return a bounded-head/tail version of one Execution snapshot."""

        del call
        output = result.output
        assert isinstance(output, dict)
        chunks = output["chunks"]
        assert isinstance(chunks, list)
        retained, omitted_chunks, omitted_bytes = _bound_chunks(chunks)
        return ToolResult(
            call_id=result.call_id,
            output={
                "ok": True,
                "exec_id": output["exec_id"],
                "status": output["status"],
                "exit_code": output.get("exit_code"),
                "is_terminal": output.get("is_terminal"),
                "truncated": output.get("truncated"),
                "next_cursor": output.get("next_cursor"),
                "available_from": output.get("available_from"),
                "chunks": retained,
                "reclaimed": {
                    "state": "snipped",
                    "omitted_chunks": omitted_chunks,
                    "omitted_bytes": omitted_bytes,
                    "retained_chunks": len(retained),
                    "reclaim": _reclaim_hint(output),
                },
            },
        )

    def prune(self, call: ToolCall, result: ToolResult) -> ToolResult:
        """Return a terminal identification-only version of a snipped snapshot."""

        del call
        output = result.output
        assert isinstance(output, dict)
        return ToolResult(
            call_id=result.call_id,
            output={
                "ok": True,
                "exec_id": output["exec_id"],
                "status": output["status"],
                "exit_code": output.get("exit_code"),
                "reclaimed": {
                    "state": "pruned",
                    "reclaim": _reclaim_hint(output),
                },
            },
        )


def _is_execution_snapshot(output: object) -> bool:
    return (
        isinstance(output, dict)
        and output.get("ok") is True
        and isinstance(output.get("exec_id"), str)
        and isinstance(output.get("status"), str)
        and isinstance(output.get("chunks"), list)
    )


def _bound_chunks(chunks: list[JSONValue]) -> tuple[list[JSONValue], int, int]:
    """Return retained chunks and omitted chunk/byte counts for one Snip."""

    omitted_chunks = 0
    omitted_bytes = 0
    if len(chunks) > SNIP_HEAD_CHUNKS + SNIP_TAIL_CHUNKS:
        omitted = chunks[SNIP_HEAD_CHUNKS : len(chunks) - SNIP_TAIL_CHUNKS]
        omitted_chunks = len(omitted)
        omitted_bytes = sum(_chunk_bytes(chunk) for chunk in omitted)
        retained = list(chunks[:SNIP_HEAD_CHUNKS]) + list(
            chunks[len(chunks) - SNIP_TAIL_CHUNKS :]
        )
    else:
        retained = list(chunks)

    for index, chunk in enumerate(retained):
        if not isinstance(chunk, dict):
            continue
        text = chunk.get("text")
        if isinstance(text, str) and len(text) > SNIP_CHUNK_MAX_CHARS:
            retained[index] = {**chunk, "text": text[:SNIP_CHUNK_MAX_CHARS]}
            omitted_bytes += len(text[SNIP_CHUNK_MAX_CHARS:].encode("utf-8"))
    return retained, omitted_chunks, omitted_bytes


def _chunk_bytes(chunk: JSONValue) -> int:
    if isinstance(chunk, dict):
        text = chunk.get("text")
        if isinstance(text, str):
            return len(text.encode("utf-8"))
    return 0


def _reclaim_hint(output: dict[str, JSONValue]) -> str:
    exec_id = output["exec_id"]
    cursor = output.get("next_cursor", 0)
    return (
        f"Re-run `output` with exec_id={exec_id!r} and cursor={cursor!r} "
        "to re-read retained output from the execution session."
    )
