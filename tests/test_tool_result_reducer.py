from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._context.tool_results import (
    SNIP_CHUNK_MAX_CHARS,
    SNIP_HEAD_CHUNKS,
    SNIP_TAIL_CHUNKS,
    _ToolResultReducer,
)

CALL = ToolCall(call_id="call_1", name="exec", arguments={"command": "pwd"})
REDUCER = _ToolResultReducer()


def _chunk(index: int, text: str = "line", stream: str = "stdout") -> dict[str, object]:
    return {
        "cursor": index,
        "stream": stream,
        "text": text,
        "timestamp": "2026-01-01T00:00:00Z",
    }


def _snapshot(
    *,
    chunk_count: int = 20,
    text: str = "line",
    exit_code: int = 0,
    status: str = "exited",
    exec_id: str = "exec_1",
) -> dict[str, object]:
    return {
        "ok": True,
        "exec_id": exec_id,
        "status": status,
        "exit_code": exit_code,
        "chunks": [_chunk(i, text) for i in range(chunk_count)],
        "next_cursor": chunk_count,
        "is_terminal": True,
        "truncated": False,
        "available_from": 0,
    }


def _result(*, output: object | None = None, error: object | None = None) -> ToolResult:
    return ToolResult(call_id=CALL.call_id, output=output, error=error)


def test_state_of_detects_monotonic_reduction_states() -> None:
    raw = _result(output=_snapshot())
    snipped = _result(
        output={
            **_snapshot(),
            "reclaimed": {"state": "snipped"},
        }
    )
    pruned = _result(
        output={
            **_snapshot(),
            "reclaimed": {"state": "pruned"},
        }
    )
    unknown = _result(output={"some": "payload"})

    assert REDUCER.state_of(raw) == "raw"
    assert REDUCER.state_of(snipped) == "snipped"
    assert REDUCER.state_of(pruned) == "pruned"
    assert REDUCER.state_of(unknown) == "raw"


def test_can_reduce_only_recognized_success_snapshots() -> None:
    assert REDUCER.can_reduce(_result(output=_snapshot()))
    assert REDUCER.can_reduce(_result(output=_snapshot(), error={"code": "x"})) is False
    assert REDUCER.can_reduce(_result(output="not a snapshot")) is False
    assert REDUCER.can_reduce(_result(output=_snapshot()["ok"])) is False
    assert REDUCER.can_reduce(_result(output=None)) is False
    assert (
        REDUCER.can_reduce(
            _result(output={**_snapshot(), "chunks": "not-a-list"}),
        )
        is False
    )
    pruned = _result(output={**_snapshot(), "reclaimed": {"state": "pruned"}})
    assert REDUCER.can_reduce(pruned) is False


def test_snip_keeps_bounded_head_and_tail_with_omission_stats() -> None:
    chunks = [_chunk(i, "line") for i in range(20)]
    snapshot = _snapshot(chunk_count=20)
    result = _result(output=snapshot)

    snipped = REDUCER.snip(result)

    assert snipped.call_id == CALL.call_id
    output = snipped.output
    assert isinstance(output, dict)
    assert output["ok"] is True
    assert output["exec_id"] == "exec_1"
    assert output["status"] == "exited"
    assert output["exit_code"] == 0
    assert output["is_terminal"] is True
    assert output["truncated"] is False
    assert output["next_cursor"] == 20
    assert output["available_from"] == 0
    retained = output["chunks"]
    assert isinstance(retained, list)
    assert retained == chunks[:SNIP_HEAD_CHUNKS] + chunks[-SNIP_TAIL_CHUNKS:]
    reclaimed = output["reclaimed"]
    assert isinstance(reclaimed, dict)
    assert reclaimed["state"] == "snipped"
    assert reclaimed["omitted_chunks"] == 10
    assert reclaimed["retained_chunks"] == 10
    assert reclaimed["omitted_bytes"] == sum(
        len(c["text"].encode()) for c in chunks[6:-4]
    )
    assert "exec_id='exec_1'" in reclaimed["reclaim"]
    assert "`output`" in reclaimed["reclaim"]


def test_snip_caps_oversized_chunk_text_without_splitting_characters() -> None:
    text = "字" * (SNIP_CHUNK_MAX_CHARS * 2)
    snapshot = _snapshot(chunk_count=1, text=text)
    result = _result(output=snapshot)

    snipped = REDUCER.snip(result)

    output = snipped.output
    assert isinstance(output, dict)
    retained = output["chunks"]
    assert isinstance(retained, list)
    retained_text = retained[0]["text"]
    assert isinstance(retained_text, str)
    assert retained_text == text[:SNIP_CHUNK_MAX_CHARS]
    assert len(retained_text) == SNIP_CHUNK_MAX_CHARS
    reclaimed = output["reclaimed"]
    assert isinstance(reclaimed, dict)
    assert reclaimed["omitted_chunks"] == 0
    assert reclaimed["omitted_bytes"] == len(
        text[SNIP_CHUNK_MAX_CHARS:].encode("utf-8")
    )


def test_snip_preserves_mixed_streams_and_short_results() -> None:
    mixed = [
        _chunk(i, "out", stream="stdout" if i % 2 == 0 else "stderr") for i in range(8)
    ]
    short_result = _result(output={**_snapshot(chunk_count=8), "chunks": mixed})

    snipped = REDUCER.snip(short_result)

    output = snipped.output
    assert isinstance(output, dict)
    assert output["chunks"] == mixed
    reclaimed = output["reclaimed"]
    assert isinstance(reclaimed, dict)
    assert reclaimed["omitted_chunks"] == 0
    assert reclaimed["omitted_bytes"] == 0


def test_prune_keeps_only_identification_and_reclaim_marker() -> None:
    result = _result(output=_snapshot(chunk_count=20))

    pruned = REDUCER.prune(result)

    assert pruned.call_id == CALL.call_id
    output = pruned.output
    assert isinstance(output, dict)
    assert output == {
        "ok": True,
        "exec_id": "exec_1",
        "status": "exited",
        "exit_code": 0,
        "reclaimed": {
            "state": "pruned",
            "reclaim": (
                "Re-run `output` with exec_id='exec_1' and cursor=20 "
                "to re-read retained output from the execution session."
            ),
        },
    }
