"""Docker Workspace Factory identity and volume binding tests.

The Docker Workspace Factory mirrors the Local Factory: it establishes a
stable Docker identity beside the project, derives the persistent volume
name from it, and fails closed on corrupted identity files. The Docker
identity is separate from the Local identity, so the same directory can
never silently rebind a Session between environments.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from cli_agent.runtime._backend.facts import _FileWriteRequest
from cli_agent.runtime._workspace import (
    _docker_volume_name,
    _DockerWorkspace,
    _DockerWorkspaceFactory,
    _load_docker_workspace_identity,
    _load_workspace_identity,
)

_DOCKER = pytest.param("docker", marks=pytest.mark.docker)


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def test_docker_identity_generates_once_and_reads_stably(tmp_path: Path) -> None:
    state = tmp_path / ".workspace"
    state.mkdir()

    first = _load_docker_workspace_identity(state)
    second = _load_docker_workspace_identity(state)

    assert first == second
    assert first.startswith("docker:")
    assert len(first) == len("docker:") + 32


def test_docker_identity_is_distinct_from_local_identity(tmp_path: Path) -> None:
    state = tmp_path / ".workspace"
    state.mkdir()

    docker_identity = _load_docker_workspace_identity(state)
    local_identity = _load_workspace_identity(state)

    assert docker_identity != local_identity
    assert docker_identity.startswith("docker:")
    assert local_identity.startswith("local:")


def test_docker_identity_fails_closed_on_corrupt_content(tmp_path: Path) -> None:
    state = tmp_path / ".workspace"
    state.mkdir()
    (state / "identity.docker").write_text("not-an-identity", encoding="utf-8")

    with pytest.raises(ValueError, match="invalid workspace identity"):
        _load_docker_workspace_identity(state)


def test_docker_volume_name_encodes_the_identity() -> None:
    volume = _docker_volume_name("docker:" + "a" * 32)

    assert volume == "cli-agent-docker-" + "a" * 32


@pytest.mark.docker
@pytest.mark.skipif(not _docker_available(), reason="Docker daemon is unavailable")
def test_docker_factory_opens_volume_and_reopen_preserves_files(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        first = await _DockerWorkspaceFactory().open(tmp_path, repertoire=None)
        assert isinstance(first, _DockerWorkspace)
        assert first.root == "/workspace"
        assert first.id.startswith("docker:")
        assert first.volume == _docker_volume_name(first.id)
        await first.filesystem.write(
            _FileWriteRequest(
                path="kept.txt",
                content=b"persisted",
            )
        )
        await first.close()

        second = await _DockerWorkspaceFactory().open(tmp_path, repertoire=None)
        try:
            assert second.id == first.id
            assert second.volume == first.volume
            assert await second.filesystem.read("kept.txt") == b"persisted"
        finally:
            await second.close()

        client = await _open_client()
        try:
            volume = await client.volumes.get(first.volume)
            await volume.delete(force=True)
        finally:
            await client.close()

    asyncio.run(scenario())


async def _open_client() -> object:
    from aiodocker import Docker

    client = Docker()
    await client.version()
    return client
