"""Shell execution driver."""

from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress

from cli_agent.runtime._environment.execution import (
    _ExecutionRecord,
    _notify_changed,
    _timestamp,
    _wait_until_terminal,
)
from cli_agent.runtime._environment.routing import _allowed_command

_OUTPUT_CHUNK_SIZE = 4096
_TERMINATE_GRACE_SECONDS = 0.5


class _ShellDriver:
    def __init__(
        self,
        *,
        output_chunk_bound: int,
        output_byte_bound: int,
    ) -> None:
        self._output_chunk_bound = output_chunk_bound
        self._output_byte_bound = output_byte_bound

    async def run(self, record: _ExecutionRecord) -> None:
        """Run one allowed Shell Execution to a terminal state."""

        command = _allowed_command(record.decision)
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_shell(
                command.raw_command,
                cwd=command.cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=os.name == "posix",
            )
            record.process = process
            record.process_ready.set()
            if record.kill_requested:
                _signal_process(process, force=False)

            stdout_task = asyncio.create_task(
                self._capture_stream(record, process.stdout, "stdout")
            )
            stderr_task = asyncio.create_task(
                self._capture_stream(record, process.stderr, "stderr")
            )
            exit_code = await process.wait()
            await asyncio.gather(stdout_task, stderr_task)
            record.exit_code = exit_code
            record.status = (
                "killed"
                if record.kill_requested
                else "exited"
                if exit_code == 0
                else "failed"
            )
        except Exception:
            if process is not None:
                _signal_process(process, force=True)
                with suppress(Exception):
                    await process.wait()
            record.status = "killed" if record.kill_requested else "failed"
            record.exit_code = process.returncode if process is not None else None
        finally:
            record.process_ready.set()

    async def terminate(self, record: _ExecutionRecord) -> None:
        """Request termination and wait for the Shell Execution task."""

        record.kill_requested = True
        await record.process_ready.wait()
        process = record.process
        if process is not None and process.returncode is None:
            _signal_process(process, force=False)
            try:
                await _wait_until_terminal(
                    record,
                    timeout=_TERMINATE_GRACE_SECONDS,
                )
            except asyncio.TimeoutError:
                _signal_process(process, force=True)

        task = record.completion_task
        if task is not None:
            with suppress(Exception):
                await task

    async def _capture_stream(
        self,
        record: _ExecutionRecord,
        stream: asyncio.StreamReader | None,
        stream_name: str,
    ) -> None:
        if stream is None:
            return

        while data := await stream.read(_OUTPUT_CHUNK_SIZE):
            if (
                len(record.chunks) >= self._output_chunk_bound
                or record.retained_bytes + len(data) > self._output_byte_bound
            ):
                record.truncated = True
                await _notify_changed(record)
                continue

            record.chunks.append(
                {
                    "cursor": len(record.chunks),
                    "stream": stream_name,
                    "text": data.decode("utf-8", errors="replace"),
                    "timestamp": _timestamp(),
                }
            )
            record.retained_bytes += len(data)
            await _notify_changed(record)


def _signal_process(
    process: asyncio.subprocess.Process,
    *,
    force: bool,
) -> None:
    if process.returncode is not None:
        return
    with suppress(ProcessLookupError):
        if os.name == "posix":
            os.killpg(
                process.pid,
                signal.SIGKILL if force else signal.SIGTERM,
            )
        elif force:
            process.kill()
        else:
            process.terminate()
