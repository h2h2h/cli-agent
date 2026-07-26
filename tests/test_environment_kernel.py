import asyncio
import json
import shlex
import sys
from pathlib import Path

import pytest

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._environment import EnvironmentKernel


def test_executes_short_command_and_retains_ordered_output(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel.create_binding()
        command = _python_command(
            "import os, sys; "
            "print(os.getcwd()); "
            "sys.stdout.flush(); "
            "sys.stderr.write('warning\\n')"
        )

        executed = await binding.dispatch(
            ToolCall(
                call_id="exec_1",
                name="exec",
                arguments={"command": command},
            )
        )
        snapshot = _output(executed)

        assert executed.call_id == "exec_1"
        assert executed.error is None
        assert snapshot["ok"] is True
        assert snapshot["status"] == "exited"
        assert snapshot["exit_code"] == 0
        assert snapshot["is_terminal"] is True
        assert snapshot["truncated"] is False
        assert snapshot["available_from"] == 0
        assert isinstance(snapshot["exec_id"], str)
        assert snapshot["exec_id"]
        assert "session_id" not in snapshot

        chunks = snapshot["chunks"]
        assert isinstance(chunks, list)
        assert [chunk["cursor"] for chunk in chunks] == list(range(len(chunks)))
        assert str(tmp_path) in _stream_text(chunks, "stdout")
        assert "warning\n" in _stream_text(chunks, "stderr")
        assert snapshot["next_cursor"] == len(chunks)
        json.dumps(snapshot)

        retained = await binding.dispatch(
            ToolCall(
                call_id="output_1",
                name="output",
                arguments={"exec_id": snapshot["exec_id"]},
            )
        )
        assert _output(retained) == snapshot

        killed = await binding.dispatch(
            ToolCall(
                call_id="kill_1",
                name="kill",
                arguments={"exec_id": snapshot["exec_id"]},
            )
        )
        killed_again = await binding.dispatch(
            ToolCall(
                call_id="kill_2",
                name="kill",
                arguments={"exec_id": snapshot["exec_id"]},
            )
        )
        assert _output(killed) == snapshot
        assert _output(killed_again) == snapshot

    asyncio.run(scenario())


def test_reports_nonzero_exit_as_terminal_execution(tmp_path: Path) -> None:
    async def scenario() -> None:
        binding = EnvironmentKernel(tmp_path).create_binding()

        result = await binding.dispatch(
            ToolCall(
                call_id="exec_nonzero",
                name="exec",
                arguments={
                    "command": _python_command(
                        "import sys; "
                        "print('before failure'); "
                        "sys.stderr.write('failure\\n'); "
                        "sys.exit(7)"
                    )
                },
            )
        )
        snapshot = _output(result)

        assert result.error is None
        assert snapshot["ok"] is True
        assert snapshot["status"] == "failed"
        assert snapshot["exit_code"] == 7
        assert snapshot["is_terminal"] is True
        chunks = snapshot["chunks"]
        assert isinstance(chunks, list)
        assert "before failure\n" in _stream_text(chunks, "stdout")
        assert "failure\n" in _stream_text(chunks, "stderr")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "call",
    (
        ToolCall(call_id="unknown", name="read", arguments={}),
        ToolCall(call_id="blank", name="exec", arguments={"command": "  "}),
        ToolCall(
            call_id="extra",
            name="exec",
            arguments={"command": "pwd", "cwd": "/"},
        ),
        ToolCall(
            call_id="bool",
            name="exec",
            arguments={"command": "pwd", "wait_ms": True},
        ),
        ToolCall(call_id="missing", name="output", arguments={}),
        ToolCall(
            call_id="cursor",
            name="output",
            arguments={"exec_id": "id", "cursor": -1},
        ),
        ToolCall(
            call_id="limit",
            name="kill",
            arguments={"exec_id": "id", "limit": 0},
        ),
    ),
)
def test_returns_structured_invalid_argument_errors(
    tmp_path: Path,
    call: ToolCall,
) -> None:
    async def scenario() -> None:
        binding = EnvironmentKernel(tmp_path).create_binding()

        result = await binding.dispatch(call)

        error = _error(result)
        assert result.call_id == call.call_id
        assert result.output is None
        assert error["ok"] is False
        assert error["code"] == "invalid_argument"
        assert isinstance(error["message"], str)
        assert error["message"]
        json.dumps(error)

    asyncio.run(scenario())


@pytest.mark.parametrize("name", ("output", "kill"))
def test_rejects_unknown_execution_ids(tmp_path: Path, name: str) -> None:
    async def scenario() -> None:
        binding = EnvironmentKernel(tmp_path).create_binding()

        result = await binding.dispatch(
            ToolCall(
                call_id=f"{name}_missing",
                name=name,
                arguments={"exec_id": "does-not-exist"},
            )
        )

        assert _error(result) == {
            "ok": False,
            "code": "unknown_execution",
            "message": "execution not found",
        }

    asyncio.run(scenario())


def test_closes_binding_and_kernel_idempotently(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        binding = kernel.create_binding()

        await binding.close()
        await binding.close()
        closed_session_result = await binding.dispatch(
            ToolCall(
                call_id="closed_session", name="exec", arguments={"command": "pwd"}
            )
        )
        assert _error(closed_session_result)["code"] == "internal"

        second_binding = kernel.create_binding()
        await kernel.close()
        await kernel.close()
        await second_binding.close()
        closed_kernel_result = await second_binding.dispatch(
            ToolCall(call_id="closed_kernel", name="exec", arguments={"command": "pwd"})
        )
        assert _error(closed_kernel_result)["code"] == "internal"

        with pytest.raises(RuntimeError, match="EnvironmentKernel is closed"):
            kernel.create_binding()

    asyncio.run(scenario())


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def _output(result: ToolResult) -> dict[str, object]:
    assert isinstance(result.output, dict)
    return result.output


def _error(result: ToolResult) -> dict[str, object]:
    assert isinstance(result.error, dict)
    return result.error


def _stream_text(chunks: list[object], stream: str) -> str:
    return "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == stream
    )
