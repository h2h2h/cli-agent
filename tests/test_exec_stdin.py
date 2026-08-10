"""exec stdin flows from the Model syscall to a real Shell subprocess."""

import asyncio
import shlex
import sys
from pathlib import Path

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._environment import EnvironmentKernel


def test_exec_schema_distinguishes_omitted_empty_and_present_stdin(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            omitted = await kernel.dispatch(
                ToolCall(
                    call_id="stdin_omitted",
                    name="exec",
                    arguments={"command": "cat"},
                )
            )
            assert omitted.error is None
            assert _output(omitted)["status"] == "exited"

            empty = await kernel.dispatch(
                ToolCall(
                    call_id="stdin_empty",
                    name="exec",
                    arguments={"command": "cat", "stdin": ""},
                )
            )
            assert empty.error is None
            assert _output(empty)["status"] == "exited"
            assert _stream_text(_output(empty)["chunks"], "stdout") == ""

            rejected = await kernel.dispatch(
                ToolCall(
                    call_id="stdin_number",
                    name="exec",
                    arguments={"command": "cat", "stdin": 42},
                )
            )
            assert _error(rejected)["code"] == "invalid_argument"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_grep_receives_stdin_from_exec(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="stdin_grep",
                    name="exec",
                    arguments={
                        "command": "grep foo",
                        "stdin": "foo\nbar\nfoobar\n",
                    },
                )
            )

            snapshot = _output(result)
            assert snapshot["status"] == "exited"
            assert snapshot["exit_code"] == 0
            assert _stream_text(snapshot["chunks"], "stdout") == "foo\nfoobar\n"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_shell_sequence_receives_stdin_once(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="stdin_sequence",
                    name="exec",
                    arguments={"command": "cat; wc -c | tr -d ' '", "stdin": "hello"},
                )
            )

            snapshot = _output(result)
            assert snapshot["status"] == "exited"
            assert _stream_text(snapshot["chunks"], "stdout") == "hello0\n"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_unicode_stdin_is_forwarded_untouched(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="stdin_unicode",
                    name="exec",
                    arguments={"command": "cat", "stdin": "héllo 世界\n"},
                )
            )

            snapshot = _output(result)
            assert snapshot["status"] == "exited"
            assert _stream_text(snapshot["chunks"], "stdout") == "héllo 世界\n"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_queued_execution_uses_stdin_bound_at_submission(tmp_path: Path) -> None:
    read_source = (
        "import sys; sys.stdout.write(sys.stdin.read())"
    )

    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path, queue_limit=4, parallel_limit=2)
        try:
            first = await _exec(
                kernel,
                _python_command("import time; time.sleep(0.2); " + read_source),
                stdin="first\n",
                wait_ms=0,
            )
            second = await _exec(
                kernel,
                _python_command(read_source),
                stdin="second\n",
                wait_ms=0,
            )
            assert _output(first)["status"] in {"running", "exited"}
            assert _output(second)["status"] == "queued"

            first_terminal = await _read_until_terminal(
                kernel, str(_output(first)["exec_id"]), cursor=0
            )
            second_terminal = await _read_until_terminal(
                kernel, str(_output(second)["exec_id"]), cursor=0
            )

            assert _stream_text(first_terminal["chunks"], "stdout") == "first\n"
            assert _stream_text(second_terminal["chunks"], "stdout") == "second\n"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_large_input_is_not_truncated(tmp_path: Path) -> None:
    payload = "x" * (1024 * 1024)

    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="stdin_large",
                    name="exec",
                    arguments={"command": "wc -c | tr -d ' '", "stdin": payload},
                )
            )

            snapshot = _output(result)
            assert snapshot["status"] == "exited"
            assert _stream_text(snapshot["chunks"], "stdout") == f"{len(payload)}\n"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_process_that_closes_stdin_early_does_not_hang(tmp_path: Path) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            result = await kernel.dispatch(
                ToolCall(
                    call_id="stdin_early_eof",
                    name="exec",
                    arguments={"command": "head -c 4", "stdin": "a" * 1024 * 1024},
                )
            )

            snapshot = _output(result)
            assert snapshot["status"] == "exited"
            assert _stream_text(snapshot["chunks"], "stdout") == "aaaa"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_batch_dispatch_carries_stdin_per_call(tmp_path: Path) -> None:
    read_source = "import sys; sys.stdout.write(sys.stdin.read())"

    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            results = await kernel.dispatch_batch(
                (
                    ToolCall(
                        call_id="batch_a",
                        name="exec",
                        arguments={
                            "command": _python_command(read_source),
                            "stdin": "alpha",
                        },
                    ),
                    ToolCall(
                        call_id="batch_b",
                        name="exec",
                        arguments={
                            "command": _python_command(read_source),
                            "stdin": "beta",
                        },
                    ),
                )
            )

            assert [result.error for result in results] == [None, None]
            first = _output(results[0])
            second = _output(results[1])
            assert first["status"] == "exited"
            assert second["status"] == "exited"
            assert _stream_text(first["chunks"], "stdout") == "alpha"
            assert _stream_text(second["chunks"], "stdout") == "beta"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_runtime_command_without_stdin_consumer_fails_clearly(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            for command in ("cd", "export A=1", "files write a.txt"):
                result = await kernel.dispatch(
                    ToolCall(
                        call_id=f"stdin_{command.split()[0]}",
                        name="exec",
                        arguments={"command": command, "stdin": "payload"},
                    )
                )
                snapshot = _output(result)
                assert snapshot["status"] == "failed"
                assert "does not consume exec stdin" in _stream_text(
                    snapshot["chunks"], "stderr"
                )

            still_works = await kernel.dispatch(
                ToolCall(
                    call_id="cd_without_stdin",
                    name="exec",
                    arguments={"command": "cd"},
                )
            )
            assert _output(still_works)["status"] == "exited"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


async def _exec(
    kernel: EnvironmentKernel,
    command: str,
    *,
    stdin: str | None = None,
    wait_ms: int = 8_000,
) -> ToolResult:
    arguments: dict[str, object] = {"command": command, "wait_ms": wait_ms}
    if stdin is not None:
        arguments["stdin"] = stdin
    return await kernel.dispatch(
        ToolCall(
            call_id=f"exec_{id(arguments)}",
            name="exec",
            arguments=arguments,
        )
    )


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


async def _read_until_terminal(
    kernel: EnvironmentKernel,
    exec_id: str,
    *,
    cursor: int,
) -> dict[str, object]:
    chunks: list[object] = []
    current_cursor = cursor
    for _ in range(10):
        result = await kernel.dispatch(
            ToolCall(
                call_id=f"output_terminal_{exec_id}_{current_cursor}",
                name="output",
                arguments={
                    "exec_id": exec_id,
                    "cursor": current_cursor,
                    "wait_ms": 500,
                },
            )
        )
        snapshot = _output(result)
        chunks.extend(snapshot["chunks"])
        current_cursor = int(snapshot["next_cursor"])
        if snapshot["is_terminal"]:
            snapshot["chunks"] = chunks
            return snapshot
    raise AssertionError("execution did not reach a terminal state")
