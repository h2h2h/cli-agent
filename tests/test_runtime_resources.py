"""Runtime resource aggregate reconciliation, rollback, and close tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from pathlib import Path

import pytest

import cli_agent.runtime._resources as resources_module
from cli_agent.runtime._backend import _BackendWorkspace
from cli_agent.runtime._capability.deployment import DeploymentSnapshot
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.provider import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilitySnapshot,
)
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.source_view import _LogicalCapabilityView
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._project_instructions import _ProjectInstructions
from cli_agent.runtime._resources import (
    _reconcile_runtime_resources,
    _RuntimeResources,
)


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _environment(workspace: Path, content: str) -> None:
    environment = workspace / ".workspace" / "env"
    environment.parent.mkdir()
    environment.write_text(content, encoding="utf-8")


def test_reconcile_returns_complete_resource_aggregate(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _environment(workspace, "TOKEN=secret\n")

    async def scenario() -> None:
        resources = await _reconcile_runtime_resources(
            workspace=workspace,
            repertoire=None,
            on_diagnostic=None,
        )

        assert isinstance(resources, _RuntimeResources)
        assert resources.workspace.root == str(workspace.resolve())
        assert resources.workspace.id.startswith("local:")
        assert isinstance(resources.backend, _BackendWorkspace)
        assert resources.backend is resources.workspace.backend
        assert resources.backend.root == str(workspace.resolve())
        assert isinstance(resources.base_env, Mapping)
        assert dict(resources.base_env) == {"TOKEN": "secret"}
        assert isinstance(resources.capability_view, _LogicalCapabilityView)
        assert isinstance(resources.deployment, DeploymentSnapshot)
        assert resources.deployment.workspace_id == resources.workspace.id
        assert len(resources.deployment.revision) == 64
        assert resources.deployment.complete, resources.deployment.error
        assert resources.snapshot.project_instructions is None
        assert isinstance(resources.snapshot.tools, _ToolCatalog)
        assert isinstance(resources.snapshot.skills, _SkillCatalog)
        assert isinstance(resources.snapshot.library, _LibraryCatalog)

    asyncio.run(scenario())


def test_reconcile_loads_workspace_agents_md_snapshot(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _environment(workspace, "TOKEN=secret\n")
    content = "# Project rules\n\nrun `uv run pytest`.\n"
    (workspace / "AGENTS.md").write_text(content, encoding="utf-8")

    async def scenario() -> None:
        resources = await _reconcile_runtime_resources(
            workspace=workspace,
            repertoire=None,
            on_diagnostic=None,
        )

        assert isinstance(resources.snapshot.project_instructions, _ProjectInstructions)
        assert resources.snapshot.project_instructions.source == str(
            workspace.resolve() / "AGENTS.md"
        )
        assert resources.snapshot.project_instructions.text == content

    asyncio.run(scenario())


def test_base_environment_is_immutable_and_excluded_from_repr(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _environment(workspace, "TOKEN=secret\n")

    async def scenario() -> None:
        resources = await _reconcile_runtime_resources(
            workspace=workspace,
            repertoire=None,
            on_diagnostic=None,
        )

        with pytest.raises(TypeError):
            resources.base_env["NEW"] = "value"  # type: ignore[index]
        representation = repr(resources)
        assert "TOKEN" not in representation
        assert "secret" not in representation

    asyncio.run(scenario())


def test_reconcile_runs_steps_in_documented_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class _FakePaths:
        root = tmp_path / "workspace"
        environment = tmp_path / "env"
        state = tmp_path / "workspace" / ".workspace"

    class _FakeBackendWorkspace:
        workspace_environment = {"TOKEN": "secret"}
        filesystem = object()

        @staticmethod
        def execution_base_environment() -> Mapping[str, str]:
            return {}

        @staticmethod
        async def flush() -> None:
            return None

        @staticmethod
        async def close() -> None:
            return None

    class _FakeWorkspace:
        def __init__(self) -> None:
            self.root = str(_FakePaths.root)
            self.root_path = _FakePaths.root
            self.backend = _FakeBackendWorkspace()
            self.filesystem = self.backend.filesystem
            self.repertoire = tmp_path / "repertoire"
            self.id = "local:00000000000000000000000000000000"

        @property
        def state_root(self) -> Path:
            return _FakePaths.state

        @property
        def deployment_volume(self) -> str:
            return ".workspace"

        @staticmethod
        async def close() -> None:
            return None

    class _FakeLocalWorkspaceFactory:
        @staticmethod
        async def open(
            workspace: str | Path,
            *,
            repertoire: str | Path | None,
        ) -> _FakeWorkspace:
            del workspace, repertoire
            order.append("workspace_open")
            return _FakeWorkspace()

    class _FakeSnapshot:
        def __init__(self, library: object | None) -> None:
            self.library = library
            self.tools = object()
            self.skills = object()
            self.revision = "fake-revision"

        def with_library(self, library: object) -> _FakeSnapshot:
            return _FakeSnapshot(library)

    class _FakeCapabilityProvider:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def discover_mcp_configs(self) -> tuple[object, ...]:
            order.append("mcp_configs")
            return ()

        async def discover(self, *, mcp_configs: object = None) -> _FakeSnapshot:
            del mcp_configs
            order.append("snapshot")
            return _FakeSnapshot(None)

    class _FakeDeployment:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        @staticmethod
        async def attach(workspace: object) -> object:
            del workspace
            order.append("view_attach")
            return object()

        @staticmethod
        async def discover_mcp(
            configs: tuple[object, ...],
            on_diagnostic: object = None,
        ) -> tuple[object, ...]:
            del configs, on_diagnostic
            order.append("mcp_discovery")
            return ()

        @staticmethod
        async def materialize_stubs(
            workspace: object,
            configs: tuple[object, ...],
            facts: tuple[object, ...],
        ) -> None:
            del workspace, configs, facts
            order.append("mcp_stubs")
            return None

        @staticmethod
        async def reconcile(snapshot: object, workspace: object) -> object:
            del workspace
            order.append("deployment_reconcile")
            return DeploymentSnapshot(
                workspace_id="local:00000000000000000000000000000000",
                revision=getattr(snapshot, "revision", "fake-revision"),
                layout_version=1,
                complete=True,
                error=None,
            )

        @staticmethod
        def executor(workspace: object, *, revision: str) -> object:
            del workspace, revision
            order.append("tool_executor")
            return object()

    class _FakeStateDatabase:
        @staticmethod
        def open() -> object:
            order.append("state_database")
            return object()

    class _FakeSummaryCache:
        def __init__(self, database: object) -> None:
            del database
            order.append("summary_cache")

    class _FakeSessionStore:
        def __init__(self, database: object) -> None:
            del database
            order.append("session_store")

    class _FakeLibraryCatalog:
        @staticmethod
        async def reconcile(
            capability_view: object,
            filesystem: object,
            summary_cache: object,
        ) -> object:
            del capability_view, filesystem, summary_cache
            order.append("library_catalog")
            return object()

    monkeypatch.setattr(
        resources_module,
        "_LocalWorkspaceFactory",
        _FakeLocalWorkspaceFactory,
    )
    monkeypatch.setattr(
        resources_module,
        "CapabilityProvider",
        _FakeCapabilityProvider,
    )
    monkeypatch.setattr(
        resources_module,
        "_LocalCapabilityDeployment",
        _FakeDeployment,
    )
    monkeypatch.setattr(resources_module, "_StateDatabase", _FakeStateDatabase)
    monkeypatch.setattr(resources_module, "_SummaryCache", _FakeSummaryCache)
    monkeypatch.setattr(resources_module, "SessionStore", _FakeSessionStore)
    monkeypatch.setattr(resources_module, "_LibraryCatalog", _FakeLibraryCatalog)

    async def scenario() -> None:
        resources = await _reconcile_runtime_resources(
            workspace=tmp_path,
            repertoire=None,
            on_diagnostic=None,
        )

        assert order == [
            "workspace_open",
            "view_attach",
            "mcp_configs",
            "mcp_discovery",
            "mcp_stubs",
            "snapshot",
            "state_database",
            "summary_cache",
            "session_store",
            "library_catalog",
            "deployment_reconcile",
            "tool_executor",
        ]
        assert resources.workspace.root == str(_FakePaths.root)
        assert dict(resources.base_env) == {"TOKEN": "secret"}

    asyncio.run(scenario())


def test_aggregate_pins_the_deployment_snapshot(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)

    async def scenario() -> None:
        await _reconcile_runtime_resources(
            workspace=workspace,
            repertoire=None,
            on_diagnostic=None,
        )

        assert {
            field.name for field in _RuntimeResources.__dataclass_fields__.values()
        } == {
            "workspace",
            "backend",
            "base_env",
            "capability_view",
            "snapshot",
            "deployment",
            "tool_executor",
            "session_store",
        }

    asyncio.run(scenario())


def test_tool_environment_failure_is_fail_soft_in_the_deployment(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    conflict = workspace / ".workspace" / ".tool-environment"
    conflict.parent.mkdir()
    conflict.write_text("not a directory\n", encoding="utf-8")

    async def scenario() -> None:
        resources = await _reconcile_runtime_resources(
            workspace=workspace,
            repertoire=None,
            on_diagnostic=None,
        )

        deployment = resources.deployment
        assert deployment.complete is False
        assert "must be a real directory" in (deployment.error or "")

    asyncio.run(scenario())


def test_reconcile_propagates_workspace_environment_failure(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _environment(workspace, "MISSING_VALUE\n")

    async def scenario() -> None:
        with pytest.raises(ValueError, match="must use KEY=VALUE"):
            await _reconcile_runtime_resources(
                workspace=workspace,
                repertoire=None,
                on_diagnostic=None,
            )

    asyncio.run(scenario())


def test_reconcile_requires_existing_workspace(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    async def scenario() -> None:
        with pytest.raises(
            ValueError,
            match="workspace must be an existing directory",
        ):
            await _reconcile_runtime_resources(
                workspace=missing,
                repertoire=None,
                on_diagnostic=None,
            )

    asyncio.run(scenario())
    assert not missing.exists()


def _package_modules(package: object) -> tuple[Path, ...]:
    import importlib

    package_path = Path(importlib.import_module(package).__file__).parent
    return tuple(path for path in package_path.rglob("*.py"))


def test_capability_package_never_imports_resources() -> None:
    for path in _package_modules("cli_agent.runtime._capability"):
        source = path.read_text(encoding="utf-8")
        assert "runtime._resources" not in source, path


def test_resources_module_never_imports_environment() -> None:
    source = Path(resources_module.__file__).read_text(encoding="utf-8")
    assert "runtime._environment" not in source


def test_environment_package_never_imports_resources() -> None:
    for path in _package_modules("cli_agent.runtime._environment"):
        source = path.read_text(encoding="utf-8")
        assert "runtime._resources" not in source, path


def test_environment_kernel_does_not_accept_resource_aggregate() -> None:
    from cli_agent.runtime._environment.kernel import EnvironmentKernel

    parameters = inspect.signature(EnvironmentKernel.__init__).parameters
    assert "resources" not in parameters
    assert "capability_view" not in parameters
    assert "backend" in parameters


class _TrackingWorkspace:
    """Fake Workspace recording open/close lifecycle events."""

    def __init__(
        self, order: list[str], *, library_failure: Exception | None = None
    ) -> None:
        self.order = order
        self.id = "local:00000000000000000000000000000000"
        self.root = "/fake"
        self.root_path = Path("/fake")
        self.workspace_environment = {"TOKEN": "secret"}
        self.backend = _TrackingBackendWorkspace(order)
        self.filesystem = self.backend.filesystem
        self.repertoire = Path("/fake/repertoire")
        self._library_failure = library_failure

    @property
    def state_root(self) -> Path:
        return Path("/fake/.workspace")

    @property
    def deployment_volume(self) -> str:
        return ".workspace"

    async def close(self) -> None:
        self.order.append("workspace.close")


class _TrackingBackendWorkspace:
    """Fake Backend recording lifecycle events on a shared list."""

    filesystem = object()

    def __init__(self, order: list[str]) -> None:
        self.order = order

    def execution_base_environment(self) -> Mapping[str, str]:
        return {}

    async def flush(self) -> None:
        self.order.append("flush")

    async def close(self) -> None:
        self.order.append("backend.close")


class _TrackingLocalWorkspaceFactory:
    """Fake Factory recording open events on a shared list."""

    order: list[str] = []

    async def open(
        self,
        workspace: str | Path,
        *,
        repertoire: str | Path | None,
    ) -> _TrackingWorkspace:
        del workspace, repertoire
        self.order.append("workspace_open")
        return _TrackingWorkspace(self.order)


class _TrackingStateDatabase:
    order: list[str] = []

    def __init__(self, order: list[str]) -> None:
        self.order = order

    @classmethod
    def open(cls) -> _TrackingStateDatabase:
        cls.order.append("db.open")
        return cls(cls.order)

    def close(self) -> None:
        self.order.append("db.close")


class _NoopSummaryCache:
    def __init__(self, database: object) -> None:
        del database


class _NoopSessionStore:
    def __init__(self, database: object) -> None:
        del database


class _NoopSnapshot:
    def __init__(self, library: object | None = None) -> None:
        self.library = library
        self.tools = object()
        self.skills = object()
        self.revision = "noop-revision"

    def with_library(self, library: object) -> _NoopSnapshot:
        return _NoopSnapshot(library)


class _NoopCapabilityProvider:
    def __init__(self, **kwargs: object) -> None:
        del kwargs

    async def discover_mcp_configs(self) -> tuple[object, ...]:
        return ()

    async def discover(self, *, mcp_configs: object = None) -> _NoopSnapshot:
        del mcp_configs
        return _NoopSnapshot()


class _NoopDeployment:
    def __init__(self, **kwargs: object) -> None:
        del kwargs

    async def attach(self, workspace: object) -> object:
        del workspace
        return object()

    async def discover_mcp(
        self,
        configs: tuple[object, ...],
        on_diagnostic: object = None,
    ) -> tuple[object, ...]:
        del configs, on_diagnostic
        return ()

    async def materialize_stubs(
        self,
        workspace: object,
        configs: tuple[object, ...],
        facts: tuple[object, ...],
    ) -> None:
        del workspace, configs, facts
        return None

    async def reconcile(self, snapshot: object, workspace: object) -> object:
        del snapshot, workspace
        return DeploymentSnapshot(
            workspace_id="local:00000000000000000000000000000000",
            revision="noop-revision",
            layout_version=1,
            complete=True,
            error=None,
        )

    @staticmethod
    def executor(workspace: object, *, revision: str) -> object:
        del workspace, revision
        return object()


class _NoopLibraryCatalog:
    @staticmethod
    async def reconcile(
        capability_view: object,
        filesystem: object,
        summary_cache: object,
    ) -> object:
        del capability_view, filesystem, summary_cache
        return object()


def _install_noop_reconcile_fakes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    order: list[str] = []
    _TrackingLocalWorkspaceFactory.order = order
    _TrackingStateDatabase.order = order
    monkeypatch.setattr(
        resources_module,
        "_LocalWorkspaceFactory",
        _TrackingLocalWorkspaceFactory,
    )
    monkeypatch.setattr(
        resources_module,
        "CapabilityProvider",
        _NoopCapabilityProvider,
    )
    monkeypatch.setattr(resources_module, "_LocalCapabilityDeployment", _NoopDeployment)
    monkeypatch.setattr(resources_module, "_StateDatabase", _TrackingStateDatabase)
    monkeypatch.setattr(resources_module, "_SummaryCache", _NoopSummaryCache)
    monkeypatch.setattr(resources_module, "SessionStore", _NoopSessionStore)
    monkeypatch.setattr(resources_module, "_LibraryCatalog", _NoopLibraryCatalog)
    return order


def test_reconcile_failure_closes_opened_resources_in_reverse_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingDeployment:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        async def attach(self, workspace: object) -> object:
            del workspace
            return object()

        async def discover_mcp(
            self,
            configs: tuple[object, ...],
            on_diagnostic: object = None,
        ) -> tuple[object, ...]:
            raise RuntimeError("mcp discovery exploded")

    order = _install_noop_reconcile_fakes(monkeypatch)
    monkeypatch.setattr(
        resources_module, "_LocalCapabilityDeployment", FailingDeployment
    )

    with pytest.raises(RuntimeError, match="mcp discovery exploded"):
        asyncio.run(
            _reconcile_runtime_resources(
                workspace=tmp_path,
                repertoire=None,
                on_diagnostic=None,
            )
        )

    assert order == ["workspace_open", "workspace.close"]


def test_reconcile_failure_at_index_publish_closes_opened_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingIndexDeployment(_NoopDeployment):
        async def reconcile(self, snapshot: object, workspace: object) -> object:
            del snapshot, workspace
            raise RuntimeError("index publish exploded")

    order = _install_noop_reconcile_fakes(monkeypatch)
    monkeypatch.setattr(
        resources_module,
        "_LocalCapabilityDeployment",
        FailingIndexDeployment,
    )

    with pytest.raises(RuntimeError, match="index publish exploded"):
        asyncio.run(
            _reconcile_runtime_resources(
                workspace=tmp_path,
                repertoire=None,
                on_diagnostic=None,
            )
        )

    assert order == ["workspace_open", "db.open", "db.close", "workspace.close"]


def test_project_instruction_load_failure_closes_opened_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _install_noop_reconcile_fakes(monkeypatch)

    class _RaisingSnapshotProvider(_NoopCapabilityProvider):
        async def discover(self, *, mcp_configs: object = None) -> _NoopSnapshot:
            del mcp_configs
            raise ValueError("AGENTS.md is not valid UTF-8")

    monkeypatch.setattr(
        resources_module,
        "CapabilityProvider",
        _RaisingSnapshotProvider,
    )

    with pytest.raises(ValueError, match="not valid UTF-8"):
        asyncio.run(
            _reconcile_runtime_resources(
                workspace=tmp_path,
                repertoire=None,
                on_diagnostic=None,
            )
        )

    assert order == ["workspace_open", "workspace.close"]


def test_workspace_open_failure_propagates_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class FailingFactory:
        async def open(
            self,
            workspace: str | Path,
            *,
            repertoire: str | Path | None,
        ) -> object:
            del workspace, repertoire
            nonlocal calls
            calls += 1
            raise ValueError("backend constraint failed")

    order = _install_noop_reconcile_fakes(monkeypatch)
    monkeypatch.setattr(
        resources_module,
        "_LocalWorkspaceFactory",
        FailingFactory,
    )

    with pytest.raises(ValueError, match="backend constraint failed"):
        asyncio.run(
            _reconcile_runtime_resources(
                workspace=tmp_path,
                repertoire=None,
                on_diagnostic=None,
            )
        )

    assert calls == 1
    assert order == []


def test_aggregate_close_follows_reverse_dependency_order(tmp_path: Path) -> None:
    order: list[str] = []

    class FakeLibraryCatalog:
        @staticmethod
        async def close() -> None:
            order.append("library.close")

    class FakeBackend:
        @staticmethod
        async def flush() -> None:
            order.append("flush")

        @staticmethod
        async def close() -> None:
            order.append("backend.close")

    class FakeWorkspace:
        def __init__(self, backend: object) -> None:
            self.backend = backend

        @staticmethod
        async def close() -> None:
            order.append("workspace.close")

    backend = FakeBackend()
    resources = _RuntimeResources(
        workspace=FakeWorkspace(backend),  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        base_env={},
        capability_view=object(),
        snapshot=_fake_snapshot(FakeLibraryCatalog()),  # type: ignore[arg-type]
        deployment=_fake_deployment(),
        tool_executor=object(),  # type: ignore[arg-type]
        session_store=object(),  # type: ignore[arg-type]
    )

    asyncio.run(resources.close())

    assert order == ["library.close", "flush", "workspace.close"]


def test_aggregate_close_attempts_every_step_and_surfaces_failure(
    tmp_path: Path,
) -> None:
    class FakeLibraryCatalog:
        @staticmethod
        async def close() -> None:
            return None

    class FailingBackend:
        def __init__(self) -> None:
            self.closed = False

        async def flush(self) -> None:
            raise RuntimeError("flush exploded")

        async def close(self) -> None:
            self.closed = True

    class FakeWorkspace:
        def __init__(self, backend: object) -> None:
            self.backend = backend

        async def close(self) -> None:
            await self.backend.close()  # type: ignore[attr-defined]

    backend = FailingBackend()
    resources = _RuntimeResources(
        workspace=FakeWorkspace(backend),  # type: ignore[arg-type]
        backend=backend,  # type: ignore[arg-type]
        base_env={},
        capability_view=object(),
        snapshot=_fake_snapshot(FakeLibraryCatalog()),  # type: ignore[arg-type]
        deployment=_fake_deployment(),
        tool_executor=object(),  # type: ignore[arg-type]
        session_store=object(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="flush exploded"):
        asyncio.run(resources.close())

    assert backend.closed


def _fake_snapshot(library: object) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        revision="test-revision",
        schema_version=CAPABILITY_SCHEMA_VERSION,
        tools=_ToolCatalog(()),
        skills=_SkillCatalog(()),
        mcp_servers=(),
        project_instructions=None,
        library=library,
    )


def _fake_deployment() -> DeploymentSnapshot:
    return DeploymentSnapshot(
        workspace_id="local:00000000000000000000000000000000",
        revision="test-revision",
        layout_version=1,
        complete=True,
        error=None,
    )
