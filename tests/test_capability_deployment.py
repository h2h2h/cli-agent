"""Issue 014: CapabilityDeployment materializes capability runtimes.

These tests pin the RFC-0014 deployment plane acceptance criteria: the
deployment is the only materialization boundary between the control plane
and the Workspace, reconcile is idempotent with a manifest-gated cache,
dependency failures keep the previous complete deployment on disk, the
DeploymentSnapshot binds the snapshot revision and Workspace identity,
and the backend-neutral publishing core runs without any Host mirror.
"""

import asyncio
import json
from pathlib import Path

import pytest

import cli_agent._adapters.local.tool_runtime as tool_runtime_module
from cli_agent._adapters.local.deployment import _LocalCapabilityDeployment
from cli_agent._adapters.local.view import _LocalCapabilityView
from cli_agent._workspaces import _LocalWorkspace
from cli_agent.runtime._backend import _FilesystemError, _FileWriteRequest
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
)
from cli_agent.runtime._capability.deployment import (
    DEPLOYMENT_MANIFEST,
    DEPLOYMENT_SCHEMA_VERSION,
    DeploymentSnapshot,
    StaleDeploymentError,
    verify_deployment,
)
from cli_agent.runtime._capability.mcp.config import discover_configs
from cli_agent.runtime._capability.mcp.facts import (
    MCPServerConfig,
    _MCPServerFacts,
    _MCPToolFacts,
)
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.snapshot import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilitySnapshot,
)
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.workspace import _prepare_workspace

_WORKSPACE_ID = "local:00000000000000000000000000000000"


def _repertoire(workspace: Path) -> Path:
    repertoire = workspace.parent / f"{workspace.name}-repertoire"
    for name in ("tools", "skills", "library", "_mcp"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    return repertoire


def _bootstrap(workspace: Path, repertoire: Path):
    """Open a Local Workspace and its Local deployment, RFC-0014 style."""

    _prepare_workspace(workspace)
    _LocalCapabilityView.materialize(workspace / ".workspace", repertoire)
    backend = _LocalBackendWorkspace(workspace, {})
    deployment = _LocalCapabilityDeployment()
    opened = _LocalWorkspace(_WORKSPACE_ID, workspace, backend, repertoire)
    return opened, deployment


def _bootstrap_opened(workspace: Path, repertoire: Path):
    opened, deployment = _bootstrap(workspace, repertoire)
    view = _LocalCapabilityView.materialize(workspace / ".workspace", repertoire)
    return opened, deployment, view


async def _snapshot(
    view,
    *,
    revision: str,
    mcp_servers: tuple[MCPServerConfig, ...] = (),
    mcp_facts: tuple[_MCPServerFacts, ...] = (),
) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        revision=revision,
        schema_version=CAPABILITY_SCHEMA_VERSION,
        tools=await _ToolCatalog.discover(view),
        skills=await _SkillCatalog.discover(view),
        mcp_servers=mcp_servers,
        project_instructions=None,
        mcp_facts=mcp_facts,
    )


class _CountingFilesystem:
    """Filesystem proxy counting writes addressed through the Workspace."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.writes: list[str] = []

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def write(self, request: _FileWriteRequest) -> object:
        self.writes.append(request.path)
        return await self._inner.write(request)


def _manifest_content(workspace: Path) -> dict[str, object]:
    return json.loads(
        (workspace / ".workspace" / DEPLOYMENT_MANIFEST).read_text(encoding="utf-8"),
    )


def _recording_sync(calls: list[str]):
    async def record_sync(
        *,
        python: Path,
        requirements: Path,
        working_directory: Path,
    ) -> None:
        del python, working_directory
        calls.append(requirements.read_text(encoding="utf-8"))

    return record_sync


def test_first_deployment_materializes_layout_and_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    (repertoire / "tools" / "math.py").write_text("def add(a, b):\n    return a + b\n")
    opened, deployment, view = _bootstrap_opened(workspace, repertoire)

    async def scenario() -> None:
        snapshot = await _snapshot(view, revision="revision-a")
        deployed = await deployment.reconcile(snapshot, opened)

        assert deployed.complete, deployed.error
        assert deployed.workspace_id == _WORKSPACE_ID
        assert deployed.revision == "revision-a"
        assert deployed.layout_version == DEPLOYMENT_SCHEMA_VERSION
        assert deployed.error is None

        environment = workspace / ".workspace" / ".tool-environment"
        assert (environment / ".venv" / "bin" / "python").is_file()
        assert (environment / "worker.py").is_file()
        assert (environment / "mcp_binding.py").is_file()
        assert (environment / "effective-requirements.txt").read_text(
            encoding="utf-8",
        ) == "mcp\n"
        assert (environment / "requirements.sha256").is_file()
        assert (workspace / ".workspace" / "tools" / "index.md").is_file()
        assert (workspace / ".workspace" / "skills" / "index.md").is_file()

        manifest = _manifest_content(workspace)
        assert manifest["schema_version"] == DEPLOYMENT_SCHEMA_VERSION
        assert manifest["workspace_id"] == _WORKSPACE_ID
        assert manifest["revision"] == "revision-a"
        assert manifest["complete"] is True
        assert set(manifest["digests"]) == {
            "stubs",
            "binding",
            "indexes",
            "worker",
            "requirements",
        }

    asyncio.run(scenario())


def test_reconcile_same_snapshot_is_a_full_cache_hit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    opened, deployment, view = _bootstrap_opened(workspace, repertoire)
    syncs: list[str] = []

    async def scenario() -> None:
        snapshot = await _snapshot(view, revision="revision-a")
        first = await deployment.reconcile(snapshot, opened)
        assert first.complete, first.error
        assert syncs == ["mcp\n"]

        counter = _CountingFilesystem(opened.filesystem)
        reopened, second_deployment, _ = _bootstrap_opened(workspace, repertoire)
        reopened.filesystem = counter
        deployed = await second_deployment.reconcile(snapshot, reopened)

        assert deployed.complete, deployed.error
        assert counter.writes == []
        assert syncs == ["mcp\n"]

    original = tool_runtime_module._sync_requirements
    tool_runtime_module._sync_requirements = _recording_sync(syncs)
    try:
        asyncio.run(scenario())
    finally:
        tool_runtime_module._sync_requirements = original


def test_revision_update_republishes_indexes_and_manifest(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    opened, deployment, view = _bootstrap_opened(workspace, repertoire)

    async def scenario() -> None:
        first = await deployment.reconcile(
            await _snapshot(view, revision="revision-a"),
            opened,
        )
        assert first.complete, first.error
        empty_index = (
            workspace / ".workspace" / "tools" / "index.md"
        ).read_text(encoding="utf-8")

        (repertoire / "tools" / "math.py").write_text(
            '"""Add numbers."""\nPARALLEL_SAFE = True\n\ndef add(a, b):\n    return a + b\n',
        )
        _, second_deployment, second_view = _bootstrap_opened(workspace, repertoire)
        second = await second_deployment.reconcile(
            await _snapshot(second_view, revision="revision-b"),
            opened,
        )

        assert second.complete, second.error
        assert second.revision == "revision-b"
        updated_index = (
            workspace / ".workspace" / "tools" / "index.md"
        ).read_text(encoding="utf-8")
        assert updated_index != empty_index
        assert "math" in updated_index
        assert _manifest_content(workspace)["revision"] == "revision-b"

    asyncio.run(scenario())


def test_dependency_failure_rolls_back_to_the_previous_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    (repertoire / "tools" / "stable.py").write_text("VALUE = 1\n")
    opened, deployment, view = _bootstrap_opened(workspace, repertoire)
    requirements = workspace / ".workspace" / "tools" / "requirements.txt"

    async def good_scenario() -> CapabilitySnapshot:
        snapshot = await _snapshot(view, revision="revision-a")
        good = await deployment.reconcile(snapshot, opened)
        assert good.complete, good.error
        return snapshot

    snapshot = asyncio.run(good_scenario())
    original = tool_runtime_module._sync_requirements
    worker = workspace / ".workspace" / ".tool-environment" / "worker.py"
    index = workspace / ".workspace" / "tools" / "index.md"
    worker_stat = worker.stat()
    index_stat = index.stat()
    manifest_before = _manifest_content(workspace)

    async def fail_sync(
        *,
        python: Path,
        requirements: Path,
        working_directory: Path,
    ) -> None:
        del python, requirements, working_directory
        raise RuntimeError("dependency synchronization failed")

    async def fail_scenario() -> None:
        requirements.write_text("unavailable-package==0\n", encoding="utf-8")
        _, failing_deployment, _ = _bootstrap_opened(workspace, repertoire)
        failed = await failing_deployment.reconcile(snapshot, opened)

        assert failed.complete is False
        assert "dependency synchronization failed" in (failed.error or "")
        assert worker.stat().st_mtime_ns == worker_stat.st_mtime_ns
        assert index.stat().st_mtime_ns == index_stat.st_mtime_ns
        assert _manifest_content(workspace) == manifest_before
        assert (
            workspace / ".workspace" / ".tool-environment" / "effective-requirements.txt"
        ).read_text(encoding="utf-8") == "unavailable-package==0\nmcp\n"

    monkeypatch.setattr(tool_runtime_module, "_sync_requirements", fail_sync)
    asyncio.run(fail_scenario())
    tool_runtime_module._sync_requirements = original

    async def recover_scenario() -> None:
        _, recovery_deployment, _ = _bootstrap_opened(workspace, repertoire)
        recovered = await recovery_deployment.reconcile(snapshot, opened)
        assert recovered.complete, recovered.error
        manifest = _manifest_content(workspace)
        assert manifest["complete"] is True
        assert manifest["revision"] == "revision-a"

    asyncio.run(recover_scenario())


def test_concurrent_reconciles_are_serialized(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    opened, deployment, view = _bootstrap_opened(workspace, repertoire)
    syncs: list[str] = []

    async def scenario() -> None:
        snapshot = await _snapshot(view, revision="revision-a")
        _, other_deployment, _other = _bootstrap_opened(workspace, repertoire)
        results = await asyncio.gather(
            deployment.reconcile(snapshot, opened),
            other_deployment.reconcile(snapshot, opened),
        )

        assert all(deployed.complete for deployed in results)
        assert syncs == ["mcp\n"]

    original = tool_runtime_module._sync_requirements
    tool_runtime_module._sync_requirements = _recording_sync(syncs)
    try:
        asyncio.run(scenario())
    finally:
        tool_runtime_module._sync_requirements = original


def test_close_and_reopen_hits_the_deployment_cache(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    opened, deployment, view = _bootstrap_opened(workspace, repertoire)
    syncs: list[str] = []

    async def scenario() -> None:
        snapshot = await _snapshot(view, revision="revision-a")
        first = await deployment.reconcile(snapshot, opened)
        assert first.complete, first.error
        await opened.backend.close()

        reopened, redeployment, reopened_view = _bootstrap_opened(workspace, repertoire)
        counter = _CountingFilesystem(reopened.filesystem)
        reopened.filesystem = counter
        deployed = await redeployment.reconcile(snapshot, reopened)

        assert deployed.complete, deployed.error
        assert counter.writes == []
        assert syncs == ["mcp\n"]

    original = tool_runtime_module._sync_requirements
    tool_runtime_module._sync_requirements = _recording_sync(syncs)
    try:
        asyncio.run(scenario())
    finally:
        tool_runtime_module._sync_requirements = original


def test_mcp_binding_and_stubs_are_deployment_domains(tmp_path: Path) -> None:
    import sys

    fixture = Path(__file__).parent / "mcp_server_fixture.py"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    directory = repertoire / "_mcp" / "math"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps(
            {
                "name": "math",
                "transport": "stdio",
                "command": [sys.executable, str(fixture)],
                "env": ["FIXTURE_TOKEN"],
            }
        ),
        encoding="utf-8",
    )
    opened, deployment, view = _bootstrap_opened(workspace, repertoire)

    async def scenario() -> None:
        configs = await discover_configs(view)
        facts = (
            _MCPServerFacts(
                name="math",
                tools=(
                    _MCPToolFacts(
                        name="add",
                        description="Add two numbers.",
                        input_schema={"type": "object"},
                    ),
                ),
            ),
        )
        snapshot = await _snapshot(
            view,
            revision="revision-a",
            mcp_servers=configs,
            mcp_facts=facts,
        )
        deployed = await deployment.reconcile(snapshot, opened)

        assert deployed.complete, deployed.error
        binding = workspace / ".workspace" / ".tool-environment" / "mcp_binding.py"
        stub = workspace / ".workspace" / "tools" / "mcp_math.py"
        assert binding.is_file()
        assert "'math':" in binding.read_text(encoding="utf-8")
        assert stub.is_file()

        manifest = _manifest_content(workspace)
        assert {"stubs", "binding"}.issubset(set(manifest["digests"]))

    asyncio.run(scenario())


def test_corrupt_manifest_is_treated_as_absent(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    opened, deployment, view = _bootstrap_opened(workspace, repertoire)
    manifest_path = workspace / ".workspace" / DEPLOYMENT_MANIFEST
    manifest_path.write_text("{not json", encoding="utf-8")

    async def scenario() -> None:
        deployed = await deployment.reconcile(
            await _snapshot(view, revision="revision-a"),
            opened,
        )

        assert deployed.complete, deployed.error
        manifest = _manifest_content(workspace)
        assert manifest["complete"] is True
        assert manifest["revision"] == "revision-a"

    asyncio.run(scenario())


def test_foreign_manifest_is_rematerialized(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    opened, deployment, view = _bootstrap_opened(workspace, repertoire)

    async def scenario() -> None:
        deployed = await deployment.reconcile(
            await _snapshot(view, revision="revision-a"),
            opened,
        )
        assert deployed.complete, deployed.error

        foreign, foreign_deployment, foreign_view = _bootstrap_opened(
            workspace,
            repertoire,
        )
        foreign.id = "local:ffffffffffffffffffffffffffffffff"
        redeployed = await foreign_deployment.reconcile(
            await _snapshot(foreign_view, revision="revision-a"),
            foreign,
        )

        assert redeployed.complete, redeployed.error
        manifest = _manifest_content(workspace)
        assert manifest["workspace_id"] == foreign.id

    asyncio.run(scenario())


def test_verify_deployment_accepts_the_matching_deployment() -> None:
    deployment = DeploymentSnapshot(
        workspace_id=_WORKSPACE_ID,
        revision="revision-a",
        layout_version=1,
        complete=True,
        error=None,
    )

    verify_deployment(
        deployment,
        revision="revision-a",
        workspace_id=_WORKSPACE_ID,
    )


def test_verify_deployment_rejects_stale_incomplete_and_foreign() -> None:
    complete = DeploymentSnapshot(
        workspace_id=_WORKSPACE_ID,
        revision="revision-a",
        layout_version=1,
        complete=True,
        error=None,
    )
    incomplete = DeploymentSnapshot(
        workspace_id=_WORKSPACE_ID,
        revision="revision-a",
        layout_version=1,
        complete=False,
        error="Tool environment is unavailable",
    )

    with pytest.raises(StaleDeploymentError, match="stale"):
        verify_deployment(
            complete,
            revision="revision-b",
            workspace_id=_WORKSPACE_ID,
        )
    with pytest.raises(StaleDeploymentError, match="different Workspace"):
        verify_deployment(
            complete,
            revision="revision-a",
            workspace_id="local:ffffffffffffffffffffffffffffffff",
        )
    with pytest.raises(StaleDeploymentError, match="did not complete"):
        verify_deployment(
            incomplete,
            revision="revision-a",
            workspace_id=_WORKSPACE_ID,
        )


class _InMemoryFilesystem:
    """Workspace Filesystem fake with no Host mirror."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    async def read(self, path: str) -> bytes:
        try:
            return self.files[path]
        except KeyError:
            raise _FilesystemError("not_found", f"no such file: {path}") from None

    async def write(self, request: _FileWriteRequest) -> object:
        self.files[request.path] = request.content
        return request

    async def remove(self, path: str, *, recursive: bool = False) -> None:
        del path, recursive
        raise AssertionError("fake deployment removes nothing")


class _FakeDeployment:
    """Protocol-conforming deployment built only on the publishing core."""

    def __init__(self, filesystem: _InMemoryFilesystem, *, volume: str) -> None:
        self._filesystem = filesystem
        self._volume = volume
        self._manifest_path = f"{volume}/deployment.json"

    async def reconcile(self, snapshot: CapabilitySnapshot, workspace) -> object:
        from cli_agent.runtime._capability.deployment import (
            _DeploymentManifest,
            domains_match,
            publish_artifacts,
            read_manifest,
            write_manifest,
        )

        artifacts = {
            f"{self._volume}/tools/index.md": snapshot.revision.encode("utf-8"),
        }
        manifest = await read_manifest(self._filesystem, self._manifest_path)
        if not domains_match(
            manifest,
            workspace_id=workspace.id,
            digests={"indexes": "digest"},
        ):
            await publish_artifacts(self._filesystem, artifacts)
            await write_manifest(
                self._filesystem,
                self._manifest_path,
                _DeploymentManifest(
                    workspace_id=workspace.id,
                    revision=snapshot.revision,
                    complete=True,
                    digests={"indexes": "digest"},
                ),
            )
        return DeploymentSnapshot(
            workspace_id=workspace.id,
            revision=snapshot.revision,
            layout_version=DEPLOYMENT_SCHEMA_VERSION,
            complete=True,
            error=None,
        )


class _FakeWorkspace:
    id = _WORKSPACE_ID
    root = "/virtual"

    def __init__(self, filesystem: _InMemoryFilesystem) -> None:
        self.filesystem = filesystem


def test_publishing_core_runs_without_a_host_mirror(tmp_path: Path) -> None:
    from cli_agent.runtime._capability.deployment import (
        CapabilityDeployment,
        artifact_digest,
        domains_match,
        read_manifest,
    )

    filesystem = _InMemoryFilesystem()
    deployment = _FakeDeployment(filesystem, volume="/virtual")
    workspace = _FakeWorkspace(filesystem)
    snapshot = CapabilitySnapshot(
        revision="revision-a",
        schema_version=CAPABILITY_SCHEMA_VERSION,
        tools=_ToolCatalog(()),
        skills=_SkillCatalog(()),
        mcp_servers=(),
        project_instructions=None,
    )

    async def scenario() -> None:
        assert isinstance(deployment, CapabilityDeployment)

        deployed = await deployment.reconcile(snapshot, workspace)
        assert deployed.complete
        assert filesystem.files["/virtual/tools/index.md"] == b"revision-a"
        manifest = await read_manifest(filesystem, "/virtual/deployment.json")
        assert manifest is not None
        assert manifest.revision == "revision-a"
        assert manifest.workspace_id == _WORKSPACE_ID
        assert domains_match(
            manifest,
            workspace_id=_WORKSPACE_ID,
            digests={"indexes": "digest"},
        )
        assert artifact_digest({"a": b"1"}) != artifact_digest({"a": b"2"})

        filesystem.files["/virtual/deployment.json"] = b"corrupt"
        assert await read_manifest(filesystem, "/virtual/deployment.json") is None

    asyncio.run(scenario())


def test_local_deployment_satisfies_the_protocol(tmp_path: Path) -> None:
    from cli_agent.runtime._capability.deployment import CapabilityDeployment

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    repertoire = _repertoire(workspace)
    opened, deployment = _bootstrap(workspace, repertoire)

    assert isinstance(deployment, CapabilityDeployment)
    assert opened.deployment_volume == ".workspace"
