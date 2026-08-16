"""Docker Shell and Tool executions: one ephemeral container per ``run()``.

``prepare`` only creates an in-memory handle; the container is created and
started when ``run()`` starts, mounts the persistent Workspace volume at
``/workspace``, receives the parsed command, the Session cwd, the merged
environment, and optional bytes stdin, and pushes stdout / stderr frames
to the sink. The container is always removed when ``run()`` finishes, so a
queued kill never creates one and a running kill leaves nothing behind.

Tool executions run the deployed venv Python over the materialized worker
inside the same ephemeral container; only the persistent capability volume
is mounted, so a worker can never reach Host-only paths. Setup executions
(materializing the private dependency environment, MCP discovery) share the
same transient-container lifecycle and return their collected output
instead of streaming it.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING, Literal

from cli_agent.runtime._backend.docker.stream import _write_stdin_eof
from cli_agent.runtime._backend.facts import _ShellExecutionRequest
from cli_agent.runtime._execution import (
    _KILLED_BEFORE_START,
    BackendExecutionError,
    ExecutionOutputSink,
    ExitStatus,
    _normalized_exit_status,
)

if TYPE_CHECKING:
    from aiodocker.containers import DockerContainer
    from aiodocker.stream import Stream

    from cli_agent.runtime._backend.docker.backend import _DockerBackendWorkspace

_STDOUT_STREAM = 1
_STDERR_STREAM = 2


class _DockerShellExecution:
    """Own one ephemeral execution container and its stream lifecycle."""

    def __init__(
        self,
        workspace: _DockerBackendWorkspace,
        request: _ShellExecutionRequest,
    ) -> None:
        self._workspace = workspace
        self._request = request
        self._container: DockerContainer | None = None
        self._run_started = False
        self._kill_requested = False
        self._start_lock = asyncio.Lock()

    async def run(self, sink: ExecutionOutputSink) -> ExitStatus:
        """Create, start, and reap one execution container exactly once.

        The container is attached before it starts so no output window is
        lost, stdin (when present) is written after start and half-closed,
        and the stream pump runs concurrently with ``wait()``. The Docker
        daemon does not reliably close an attached stream at container
        exit, so the pump is drained for a bounded window after the
        terminal event. The container is removed on every path: normal
        exit, signal termination, daemon failure, consumer cancellation,
        and a kill racing with creation.
        """

        if self._run_started:
            raise RuntimeError("ExecutionHandle.run called more than once")
        self._run_started = True
        if self._kill_requested:
            return ExitStatus(_KILLED_BEFORE_START)

        request = self._request
        container: DockerContainer | None = None
        stream: Stream | None = None
        pump: asyncio.Task[None] | None = None
        try:
            try:
                container = await self._workspace._create_execution_container(
                    request,
                    stdin=request.input_data is not None,
                )
                self._container = container
                self._workspace._track_container(container.id)
                if self._kill_requested:
                    with suppress(Exception):
                        await container.delete(force=True)
                    self._workspace._untrack_container(container.id)
                    container = None
                    return ExitStatus(_KILLED_BEFORE_START)
                stream = container.attach(
                    stdin=request.input_data is not None,
                    stdout=True,
                    stderr=True,
                )
                # aiodocker establishes the hijacked connection lazily on
                # the first read/write; connect before start so a fast
                # command never exits before its output stream exists.
                await stream._init()  # type: ignore[attr-defined]
                async with self._start_lock:
                    if self._kill_requested:
                        with suppress(Exception):
                            await container.delete(force=True)
                        self._workspace._untrack_container(container.id)
                        container = None
                        return ExitStatus(_KILLED_BEFORE_START)
                    await container.start()
                if request.input_data is not None:
                    await stream.write_in(request.input_data)
                    _write_stdin_eof(stream)
                pump = asyncio.create_task(_pump_stream(stream, sink))
                status = await container.wait()
                return _normalized_exit_status(int(status.get("StatusCode", 0)))
            finally:
                await _drain_stream(pump, stream)
        except asyncio.CancelledError:
            if container is not None:
                with suppress(Exception):
                    await container.kill(signal="SIGKILL")
            raise
        except BackendExecutionError:
            raise
        except Exception as exc:
            raise BackendExecutionError("Docker execution failed") from exc
        finally:
            if container is not None:
                with suppress(Exception):
                    await container.delete(force=True)
                self._workspace._untrack_container(container.id)

    async def kill(self) -> None:
        """Terminate the execution container idempotently at any time."""

        self._kill_requested = True
        async with self._start_lock:
            container = self._container
            if container is None:
                return
            with suppress(Exception):
                await container.kill(signal="SIGKILL")


class _DockerToolExecution:
    """Own one ephemeral Tool worker container and its stream lifecycle.

    The worker command (the deployed venv Python over the materialized
    worker) starts only in ``run``; the container mounts the persistent
    Workspace volume and nothing else, so the worker payload and its
    bindings stay entirely inside the volume namespace.
    """

    def __init__(
        self,
        workspace: _DockerBackendWorkspace,
        *,
        python: str,
        worker: str,
        payload: bytes,
        cwd: str,
        environment: dict[str, str],
    ) -> None:
        self._workspace = workspace
        self._python = python
        self._worker = worker
        self._payload = payload
        self._cwd = cwd
        self._environment = environment
        self._container: DockerContainer | None = None
        self._run_started = False
        self._kill_requested = False
        self._start_lock = asyncio.Lock()

    async def run(self, sink: ExecutionOutputSink) -> ExitStatus:
        """Create, start, and reap one Tool worker container exactly once."""

        if self._run_started:
            raise RuntimeError("ExecutionHandle.run called more than once")
        self._run_started = True
        if self._kill_requested:
            return ExitStatus(_KILLED_BEFORE_START)

        workspace = self._workspace
        container: DockerContainer | None = None
        stream: Stream | None = None
        pump: asyncio.Task[None] | None = None
        try:
            try:
                config = workspace._tool_container_config(
                    python=self._python,
                    worker=self._worker,
                    cwd=self._cwd,
                    environment=self._environment,
                )
                container = await workspace._create_container(config, stdin=True)
                self._container = container
                workspace._track_container(container.id)
                if self._kill_requested:
                    with suppress(Exception):
                        await container.delete(force=True)
                    workspace._untrack_container(container.id)
                    container = None
                    return ExitStatus(_KILLED_BEFORE_START)
                stream = container.attach(stdin=True, stdout=True, stderr=True)
                await stream._init()  # type: ignore[attr-defined]
                async with self._start_lock:
                    if self._kill_requested:
                        with suppress(Exception):
                            await container.delete(force=True)
                        workspace._untrack_container(container.id)
                        container = None
                        return ExitStatus(_KILLED_BEFORE_START)
                    await container.start()
                await stream.write_in(self._payload)
                _write_stdin_eof(stream)
                pump = asyncio.create_task(_pump_stream(stream, sink))
                status = await container.wait()
                return _normalized_exit_status(int(status.get("StatusCode", 0)))
            finally:
                await _drain_stream(pump, stream)
        except asyncio.CancelledError:
            if container is not None:
                with suppress(Exception):
                    await container.kill(signal="SIGKILL")
            raise
        except BackendExecutionError:
            raise
        except Exception as exc:
            raise BackendExecutionError("Docker Tool execution failed") from exc
        finally:
            if container is not None:
                with suppress(Exception):
                    await container.delete(force=True)
                workspace._untrack_container(container.id)

    async def kill(self) -> None:
        """Terminate the worker container idempotently at any time."""

        self._kill_requested = True
        async with self._start_lock:
            container = self._container
            if container is None:
                return
            with suppress(Exception):
                await container.kill(signal="SIGKILL")


async def _run_docker_setup(
    workspace: _DockerBackendWorkspace,
    *,
    command: str,
    environment: dict[str, str],
    input_data: bytes | None = None,
    mount_workspace: bool = True,
) -> tuple[int, str]:
    """Run one transient setup execution and return its exit code and output.

    The setup container mounts the persistent Workspace volume, runs one
    ``/bin/sh -c`` command (optionally consuming bytes stdin), and is
    always removed when the run finishes. The volume keeps every
    materialized artifact; the setup container never becomes an implicit
    owner of later executions.

    Returns:
        The normalized exit code and the collected output text.

    Raises:
        BackendExecutionError: If container creation or the execution
            itself fails; a non-zero setup exit code is a result, not an
            error.
    """

    config = workspace._setup_container_config(
        command=command,
        environment=environment,
        mount_workspace=mount_workspace,
    )
    container = await workspace._create_container(config, stdin=input_data is not None)
    workspace._track_container(container.id)
    stream: Stream | None = None
    pump: asyncio.Task[None] | None = None
    chunks: list[bytes] = []
    try:
        if input_data is not None:
            stream = container.attach(stdin=True, stdout=True, stderr=True)
            await stream._init()  # type: ignore[attr-defined]
        await container.start()
        if input_data is not None and stream is not None:
            await stream.write_in(input_data)
            _write_stdin_eof(stream)
            pump = asyncio.create_task(_collect_stream(stream, chunks))
        status = await container.wait()
        exit_code = int(status.get("StatusCode", 0))
        await _drain_stream(pump, stream)
        return exit_code, b"".join(chunks).decode("utf-8", errors="replace")
    except asyncio.CancelledError:
        with suppress(Exception):
            await container.kill(signal="SIGKILL")
        raise
    except Exception as exc:
        raise BackendExecutionError("Docker setup execution failed") from exc
    finally:
        if stream is not None:
            with suppress(Exception):
                await stream.close()
        with suppress(Exception):
            await container.delete(force=True)
        workspace._untrack_container(container.id)


async def _drain_stream(
    pump: asyncio.Task[None] | None,
    stream: Stream | None,
) -> None:
    """Drain the pump for a bounded window, then release the stream.

    The container is terminal at this point, so every output byte already
    exists in the daemon or socket buffers; a short drain window is
    generous for the tail while still failing closed instead of hanging on
    a daemon that never closes the stream.
    """

    if pump is not None:
        try:
            await asyncio.wait_for(asyncio.shield(pump), timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pump.cancel()
            with suppress(asyncio.CancelledError):
                await pump
        except Exception:
            pump.cancel()
            with suppress(Exception):
                await pump
    if stream is not None:
        with suppress(Exception):
            await stream.close()


async def _pump_stream(
    stream: Stream,
    sink: ExecutionOutputSink,
) -> None:
    """Forward hijacked stdout / stderr frames to the output sink."""

    while True:
        message = await stream.read_out()
        if message is None:
            return
        stream_type, data = message
        if data:
            await sink.write(_stream_name(stream_type), data)


async def _collect_stream(stream: Stream, chunks: list[bytes]) -> None:
    """Collect every hijacked stdout / stderr frame into one byte list."""

    while True:
        message = await stream.read_out()
        if message is None:
            return
        _, data = message
        if data:
            chunks.append(data)


def _stream_name(stream_type: int) -> Literal["stdout", "stderr"]:
    if stream_type == _STDERR_STREAM:
        return "stderr"
    return "stdout"
