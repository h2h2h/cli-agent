"""Docker Backend: containerized Workspace execution and volume filesystem.

The Docker Backend implements the same ``_BackendWorkspace`` seam as the
Local Backend: every ordinary Shell execution runs in one ephemeral
container that mounts a persistent Workspace volume at ``/workspace``, and
every filesystem operation executes inside a long-lived helper container
over the same volume. ``open_workspace`` fails closed when the daemon is
unreachable, the image is missing and cannot be pulled, or the volume
cannot be provisioned; execution-time daemon failures surface as
``BackendExecutionError`` and never masquerade as command exit codes.

Capability materialization and Tool worker execution (RFC-0017) live in
the Docker CapabilityDeployment: the deployment attaches the materialized
Tool Runtime to this Backend through the Docker-only ``_tool_runtime``
seam, and every execution mounts the persistent capability volume without
mapping any Host path into the container namespace.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiodocker import Docker
from aiodocker.exceptions import DockerError
from aiodocker.types import JSONObject, JSONValue

from cli_agent.runtime._backend.docker.execution import _DockerShellExecution
from cli_agent.runtime._backend.docker.filesystem import (
    _FS_SERVER,
    _DockerWorkspaceFilesystem,
)
from cli_agent.runtime._backend.docker.stream import _write_stdin_eof
from cli_agent.runtime._backend.facts import (
    _FilesystemError,
    _ShellExecutionRequest,
)
from cli_agent.runtime._execution import BackendExecutionError, ExecutionHandle

if TYPE_CHECKING:
    from aiodocker.containers import DockerContainer

    from cli_agent.runtime._backend.docker.deployment import _DockerToolRuntime

_NOT_FOUND = 404


@dataclass(frozen=True, slots=True)
class _DockerWorkspaceSource:
    """Backend-neutral input facts for opening one Docker Workspace.

    Host-side facts (control directory, environment file, Repertoire) are
    consumed by the Docker Workspace Factory before open; the Backend only
    sees the provisioned volume name, the execution image, the
    container-native root, and the parsed Workspace environment.
    """

    volume: str
    image: str
    root: str
    environment: Mapping[str, str]


class _DockerBackend:
    """Open one Docker Backend Workspace over a persistent volume."""

    async def open_workspace(
        self,
        source: _DockerWorkspaceSource,
    ) -> _DockerBackendWorkspace:
        """Open the Docker Workspace; any open failure must fail closed.

        Args:
            source (`_DockerWorkspaceSource`):
                Provisioned volume, image, root, and environment facts.

        Returns:
            The opened Docker Backend Workspace.

        Raises:
            ValueError: If the daemon is unreachable, the image cannot be
                resolved, or the volume / helper container cannot be
                provisioned.
        """

        client = Docker()
        try:
            await client.version()
        except Exception as exc:
            await _close_client(client)
            raise ValueError(
                "Docker daemon is unavailable; check that Docker is running "
                "and reachable"
            ) from exc
        try:
            await _ensure_image(client, source.image)
            volume = await _ensure_volume(client, source.volume)
            helper = await _ensure_fs_helper(
                client,
                helper_name=_fs_helper_name(source.volume),
                image=source.image,
                volume=source.volume,
                root=source.root,
            )
        except Exception as exc:
            await _close_client(client)
            if isinstance(exc, ValueError):
                raise
            raise ValueError(f"cannot open Docker Workspace: {exc}") from exc
        return _DockerBackendWorkspace(client, source, volume, helper)


class _DockerBackendWorkspace:
    """One live Docker Backend Workspace shared by every Session Kernel."""

    def __init__(
        self,
        client: Docker,
        source: _DockerWorkspaceSource,
        volume: str,
        helper: DockerContainer,
    ) -> None:
        self.root = source.root
        self.workspace_environment = dict(source.environment)
        self._client = client
        self._source = source
        self._volume = volume
        self._helper = helper
        self._live_containers: set[str] = set()
        self._tool_runtime: _DockerToolRuntime | None = None
        self._closed = False
        self.filesystem = _DockerWorkspaceFilesystem(self)

    @property
    def volume(self) -> str:
        """Return the persistent volume name backing this Workspace."""

        return self._volume

    def execution_base_environment(self) -> Mapping[str, str]:
        """Return the Workspace environment merged under every execution.

        Docker containers receive the image's own environment by default;
        the Workspace environment overlays it and the Session environment
        overlays that. The Host ambient environment never leaks in.
        """

        self._ensure_open()
        return dict(self.workspace_environment)

    def prepare_shell(
        self,
        request: _ShellExecutionRequest,
    ) -> ExecutionHandle:
        """Prepare one Shell execution without creating a container."""

        self._ensure_open()
        return _DockerShellExecution(self, request)

    async def flush(self) -> None:
        """Volume writes are immediately durable; nothing to flush."""

        self._ensure_open()

    async def close(self) -> None:
        """Remove transient containers and release the daemon connection.

        The durable Workspace volume is preserved. Every step is attempted
        so a failure cannot leak a container; the first failure is raised
        so the Host never assumes cleanup succeeded.
        """

        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        for container_id in tuple(self._live_containers):
            try:
                container = self._client.containers.container(container_id)
                with suppress(Exception):
                    await container.kill(signal="SIGKILL")
                await container.delete(force=True)
            except Exception as exc:
                errors.append(exc)
        self._live_containers.clear()
        try:
            await self._helper.delete(force=True)
        except Exception as exc:
            errors.append(exc)
        await self._close_client()
        if errors:
            raise errors[0]

    async def _close_client(self) -> None:
        await _close_client(self._client)

    def _ensure_open(self) -> None:
        """Reject every operation after this Backend Workspace closes."""

        if self._closed:
            raise RuntimeError("Backend Workspace is closed")

    def _track_container(self, container_id: str) -> None:
        """Register one live execution container for close-time reaping."""

        self._live_containers.add(container_id)

    def _untrack_container(self, container_id: str) -> None:
        """Deregister one reaped execution container."""

        self._live_containers.discard(container_id)

    def _attach_tool_runtime(self, runtime: _DockerToolRuntime) -> None:
        """Bind one materialized Docker Tool Runtime (Docker-only seam).

        The Backend public protocol never exposes Tool Runtime mechanics;
        the Docker CapabilityDeployment uses this seam to record the venv
        Python, worker, and binding paths the ToolExecutor consumes.
        """

        self._ensure_open()
        self._tool_runtime = runtime

    async def _create_execution_container(
        self,
        request: _ShellExecutionRequest,
        *,
        stdin: bool,
    ) -> DockerContainer:
        """Create one execution container without starting it.

        Container creation or image resolution failures raise
        ``BackendExecutionError``; command semantic failures stay exit
        codes and never reach this path.
        """

        self._ensure_open()
        source = self._source
        environment = {
            **self.workspace_environment,
            **request.environment,
        }
        config: dict[str, JSONValue] = {
            "Image": source.image,
            "Cmd": ["/bin/sh", "-c", request.command.raw_command],
            "WorkingDir": request.cwd,
            "Env": [f"{key}={value}" for key, value in environment.items()],
            "Tty": False,
            "HostConfig": {
                "Binds": [f"{self._volume}:{source.root}"],
            },
        }
        return await self._create_container(config, stdin=stdin)

    async def _create_container(
        self,
        config: JSONObject,
        *,
        stdin: bool,
    ) -> DockerContainer:
        """Create one container from raw config without starting it.

        Container creation or image resolution failures raise
        ``BackendExecutionError``; command semantic failures stay exit
        codes and never reach this path.
        """

        self._ensure_open()
        payload: dict[str, JSONValue] = {
            **config,
            "OpenStdin": stdin,
            "StdinOnce": stdin,
            "Tty": False,
            "AttachStdin": stdin,
            "AttachStdout": True,
            "AttachStderr": True,
        }
        try:
            return await self._client.containers.create(payload)
        except Exception as exc:
            raise BackendExecutionError("Docker container creation failed") from exc

    def _tool_container_config(
        self,
        *,
        python: str,
        worker: str,
        cwd: str,
        environment: dict[str, str],
    ) -> JSONObject:
        """Return the ephemeral Tool worker container config.

        Only the persistent Workspace volume is mounted: the worker and
        its bindings live entirely inside the volume namespace, and no
        Host path is ever mapped into the container.
        """

        self._ensure_open()
        source = self._source
        return {
            "Image": source.image,
            "Cmd": [python, worker],
            "WorkingDir": cwd,
            "Env": [f"{key}={value}" for key, value in environment.items()],
            "HostConfig": {
                "Binds": [f"{self._volume}:{source.root}"],
            },
        }

    def _setup_container_config(
        self,
        *,
        command: str,
        environment: dict[str, str],
    ) -> JSONObject:
        """Return one transient setup container config over the volume."""

        self._ensure_open()
        source = self._source
        return {
            "Image": source.image,
            "Cmd": ["/bin/sh", "-c", command],
            "WorkingDir": source.root,
            "Env": [f"{key}={value}" for key, value in environment.items()],
            "HostConfig": {
                "Binds": [f"{self._volume}:{source.root}"],
            },
        }

    async def _fs_call(self, payload: dict[str, object]) -> dict[str, object]:
        """Run one filesystem request inside the helper container.

        Returns:
            The raw helper response dict (``ok`` may be false).

        Raises:
            `_FilesystemError`: If the helper transport fails, the exec
                itself fails, or the response is not valid JSON.
        """

        self._ensure_open()
        execution = await self._helper.exec(
            ["python3", "-c", _FS_SERVER],
            stdin=True,
            stdout=True,
            stderr=True,
        )
        stream = execution.start()
        try:
            await stream.write_in(_encode_json(payload))
            _write_stdin_eof(stream)
            stdout = b""
            stderr = b""
            while True:
                message = await stream.read_out()
                if message is None:
                    break
                stream_type, data = message
                if not data:
                    continue
                if stream_type == 2:
                    stderr += data
                else:
                    stdout += data
            info = await execution.inspect()
        except Exception as exc:
            raise _FilesystemError(
                "internal", "container filesystem helper failed"
            ) from exc
        finally:
            with suppress(Exception):
                await stream.close()
        if int(info.get("ExitCode", -1)) != 0:
            raise _FilesystemError(
                "internal",
                "container filesystem helper crashed",
            )
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise _FilesystemError(
                "internal", "container filesystem helper returned invalid output"
            ) from exc
        if not isinstance(response, dict):
            raise _FilesystemError(
                "internal", "container filesystem helper returned invalid output"
            )
        return response


async def _ensure_image(client: Docker, image: str) -> None:
    """Resolve one execution image, pulling it when missing."""
    try:
        await client.images.inspect(image)
        return
    except DockerError as exc:
        if exc.status != _NOT_FOUND:
            raise ValueError(f"cannot inspect Docker image {image}: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"cannot inspect Docker image {image}: {exc}") from exc
    try:
        await client.images.pull(image)
    except Exception as exc:
        raise ValueError(f"cannot pull Docker image {image}: {exc}") from exc


async def _ensure_volume(client: Docker, volume: str) -> str:
    """Return one durable Workspace volume, creating it when missing."""
    try:
        existing = await client.volumes.get(volume)
        await existing.show()
        return volume
    except DockerError as exc:
        if exc.status != _NOT_FOUND:
            raise ValueError(f"cannot inspect Docker volume {volume}: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"cannot inspect Docker volume {volume}: {exc}") from exc
    try:
        created = await client.volumes.create({"Name": volume})
    except Exception as exc:
        raise ValueError(f"cannot create Docker volume {volume}: {exc}") from exc
    return created.name


async def _ensure_fs_helper(
    client: Docker,
    *,
    helper_name: str,
    image: str,
    volume: str,
    root: str,
) -> DockerContainer:
    """Return one running helper container over the Workspace volume.

    A stopped or missing helper is replaced; a running helper left behind
    by a crashed process is reused, so reopens of the same volume attach
    to the same container namespace.
    """

    try:
        helper = await client.containers.get(helper_name)
        info = await helper.show()
        if bool(info.get("State", {}).get("Running")):
            return helper
        with suppress(Exception):
            await helper.delete(force=True)
    except DockerError as exc:
        if exc.status != _NOT_FOUND:
            raise ValueError(f"cannot inspect helper container: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"cannot inspect helper container: {exc}") from exc
    try:
        container = await client.containers.create(
            {
                "Image": image,
                "Cmd": ["tail", "-f", "/dev/null"],
                "WorkingDir": root,
                "Tty": False,
                "OpenStdin": False,
                "HostConfig": {
                    "Binds": [f"{volume}:{root}"],
                },
            },
            name=helper_name,
        )
        await container.start()
        return container
    except Exception as exc:
        raise ValueError(f"cannot start filesystem helper container: {exc}") from exc


def _fs_helper_name(volume: str) -> str:
    """Return the deterministic helper container name for one volume."""
    return f"cli-agent-fs-{volume}"


def _encode_json(payload: dict[str, object]) -> bytes:
    return (json.dumps(payload) + "\n").encode("utf-8")


async def _close_client(client: Docker) -> None:
    """Close one aiodocker session, best effort."""
    with suppress(Exception):
        await client.close()
