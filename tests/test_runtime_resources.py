"""Runtime resource aggregate reconciliation, rollback, and close tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

import cli_agent.runtime._resources as resources_module
from cli_agent.presets import local_runtime_components
from cli_agent.runtime import ContextPolicy, WorkspaceConfig
from cli_agent.runtime._capability.deployment import DeploymentSnapshot
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.snapshot import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilitySnapshot,
)
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._project_instructions import _ProjectInstructions
from cli_agent.runtime._resources import (
    CapabilityBinding,
    _reconcile_runtime_resources,
    _RuntimeResources,
)

_CONTEXT_POLICY = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def _components(state_path: Path):
    return local_runtime_components(
        interaction=_ScriptedInteraction("allow_once"),
        context_policy=_CONTEXT_POLICY,
        state_path=state_path,
    )


async def _reconcile(
    workspace: Path,
    *,
    repertoire: Path | None = None,
):
    return await _reconcile_runtime_resources(
        config=WorkspaceConfig(root=workspace, repertoire=repertoire),
        components=_components(workspace.parent / f"{workspace.name}-state.sqlite3"),
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
        resources = await _reconcile(workspace)

        assert isinstance(resources, _RuntimeResources)
        assert resources.workspace.root == str(workspace.resolve())
        assert resources.workspace.id.startswith("local:")
        assert not hasattr(resources, "backend")
        assert resources.workspace.backend.root == str(workspace.resolve())
        assert dict(resources.workspace.base_environment) == {"TOKEN": "secret"}
        assert resources.capabilities.snapshot.project_instructions is None
        assert isinstance(resources.capabilities.snapshot.tools, _ToolCatalog)
        assert isinstance(resources.capabilities.snapshot.skills, _SkillCatalog)
        assert isinstance(resources.capabilities.snapshot.library, _LibraryCatalog)
        assert not hasattr(resources, "capability_view")
        assert not hasattr(resources, "deployment")
        await resources.close()

    asyncio.run(scenario())


def test_reconcile_loads_workspace_agents_md_snapshot(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _environment(workspace, "TOKEN=secret\n")
    content = "# Project rules\n\nrun `uv run pytest`.\n"
    (workspace / "AGENTS.md").write_text(content, encoding="utf-8")

    async def scenario() -> None:
        resources = await _reconcile(workspace)

        snapshot = resources.capabilities.snapshot
        assert isinstance(snapshot.project_instructions, _ProjectInstructions)
        assert snapshot.project_instructions.source == str(
            workspace.resolve() / "AGENTS.md"
        )
        assert snapshot.project_instructions.text == content
        await resources.close()

    asyncio.run(scenario())


def test_base_environment_is_immutable_and_excluded_from_repr(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _environment(workspace, "TOKEN=secret\n")

    async def scenario() -> None:
        resources = await _reconcile(workspace)

        with pytest.raises(TypeError):
            resources.workspace.base_environment["NEW"] = "value"  # type: ignore[index]
        representation = repr(resources)
        assert "TOKEN" not in representation
        assert "secret" not in representation
        await resources.close()

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
        def base_environment(self) -> Mapping[str, str]:
            return self.backend.workspace_environment

        @staticmethod
        async def flush() -> None:
            return None

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
        async def discover(
            self,
            source: object,
            *,
            mcp_discovery: object,
            mcp_environment: object,
            project_instructions: object,
        ) -> _FakeSnapshot:
            del project_instructions
            await mcp_discovery.discover((), source, mcp_environment)  # type: ignore[attr-defined]
            order.append("snapshot")
            return _FakeSnapshot(None)

    class _FakeSourceFactory:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        @staticmethod
        async def create(workspace: object) -> object:
            del workspace
            order.append("source_create")
            return object()

    class _FakeMCPDiscovery:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        @staticmethod
        async def discover(
            configs: tuple[object, ...],
            source: object,
            workspace: object,
        ) -> tuple[object, ...]:
            del configs, source, workspace
            order.append("mcp_discovery")
            return ()

    class _FakeDeployment:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

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

    class _FakeOverlay:
        @staticmethod
        async def close() -> None:
            return None

    class _FakeOverlayFactory:
        @staticmethod
        async def create(workspace: object) -> _FakeOverlay:
            del workspace
            order.append("overlay_materialize")
            return _FakeOverlay()

    class _FakeExecutorFactory:
        @staticmethod
        def create(
            workspace: object,
            snapshot: object,
            deployment: object,
        ) -> object:
            del workspace, snapshot, deployment
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
            return _FakeLibraryCatalog()

        async def close(self) -> None:
            return None

    monkeypatch.setattr(resources_module, "_LibraryCatalog", _FakeLibraryCatalog)
    components = replace(
        _components(tmp_path / "state.sqlite3"),
        workspace_factory=_FakeLocalWorkspaceFactory(),
        capability_source_factory=_FakeSourceFactory(),
        capability_provider=_FakeCapabilityProvider(),
        mcp_discovery=_FakeMCPDiscovery(),
        capability_deployment=_FakeDeployment(),
        capability_overlay_factory=_FakeOverlayFactory(),
        tool_executor_factory=_FakeExecutorFactory(),
    )

    async def scenario() -> None:
        resources = await _reconcile_runtime_resources(
            config=WorkspaceConfig(root=tmp_path),
            components=components,
        )

        assert order == [
            "workspace_open",
            "source_create",
            "mcp_discovery",
            "snapshot",
            "overlay_materialize",
            "library_catalog",
            "deployment_reconcile",
            "tool_executor",
        ]
        assert resources.workspace.root == str(_FakePaths.root)
        assert dict(resources.workspace.base_environment) == {"TOKEN": "secret"}
        await resources.close()

    asyncio.run(scenario())


def test_aggregate_keeps_only_consumed_capability_binding(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)

    async def scenario() -> None:
        resources = await _reconcile(workspace)

        assert {
            field.name for field in _RuntimeResources.__dataclass_fields__.values()
        } == {
            "workspace",
            "capabilities",
            "session_store",
        }
        assert set(CapabilityBinding.__dataclass_fields__) == {
            "snapshot",
            "tool_executor",
            "overlay",
        }
        await resources.close()

    asyncio.run(scenario())


def test_tool_environment_failure_is_fail_soft_in_the_deployment(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    conflict = workspace / ".workspace" / ".tool-environment"
    conflict.parent.mkdir()
    conflict.write_text("not a directory\n", encoding="utf-8")

    async def scenario() -> None:
        captured: list[DeploymentSnapshot] = []

        class CapturingExecutorFactory:
            def create(
                self,
                workspace: object,
                snapshot: object,
                deployment: DeploymentSnapshot,
            ) -> object:
                del workspace, snapshot
                captured.append(deployment)
                return object()

        components = replace(
            _components(tmp_path / "state.sqlite3"),
            tool_executor_factory=CapturingExecutorFactory(),
        )
        resources = await _reconcile_runtime_resources(
            config=WorkspaceConfig(root=workspace),
            components=components,
        )

        assert captured[0].complete is False
        assert "must be a real directory" in (captured[0].error or "")
        await resources.close()

    asyncio.run(scenario())


def test_reconcile_propagates_workspace_environment_failure(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    _environment(workspace, "MISSING_VALUE\n")

    async def scenario() -> None:
        with pytest.raises(ValueError, match="must use KEY=VALUE"):
            await _reconcile(workspace)

    asyncio.run(scenario())


def test_reconcile_requires_existing_workspace(tmp_path: Path) -> None:
    missing = tmp_path / "missing"

    async def scenario() -> None:
        with pytest.raises(
            ValueError,
            match="workspace must be an existing directory",
        ):
            await _reconcile(missing)

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
    assert "workspace" in parameters
    assert "backend" not in parameters


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
    def base_environment(self) -> Mapping[str, str]:
        return self.workspace_environment

    def execution_base_environment(self) -> Mapping[str, str]:
        return {}

    @property
    def state_root(self) -> Path:
        return Path("/fake/.workspace")

    @property
    def deployment_volume(self) -> str:
        return ".workspace"

    async def close(self) -> None:
        self.order.append("workspace.close")

    async def flush(self) -> None:
        await self.backend.flush()


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
    async def discover(
        self,
        source: object,
        *,
        mcp_discovery: object,
        mcp_environment: object,
        project_instructions: object,
    ) -> _NoopSnapshot:
        del project_instructions
        await mcp_discovery.discover((), source, mcp_environment)  # type: ignore[attr-defined]
        return _NoopSnapshot()


class _NoopSourceFactory:
    def __init__(self, **kwargs: object) -> None:
        del kwargs

    @staticmethod
    async def create(workspace: object) -> object:
        del workspace
        return object()


class _NoopMCPDiscovery:
    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    @staticmethod
    async def discover(
        configs: tuple[object, ...],
        source: object,
        workspace: object,
    ) -> tuple[object, ...]:
        del configs, source, workspace
        return ()


class _NoopDeployment:
    def __init__(self, **kwargs: object) -> None:
        del kwargs

    async def reconcile(self, snapshot: object, workspace: object) -> object:
        del snapshot, workspace
        return DeploymentSnapshot(
            workspace_id="local:00000000000000000000000000000000",
            revision="noop-revision",
            layout_version=1,
            complete=True,
            error=None,
        )


class _NoopOverlay:
    order: list[str] = []

    async def close(self) -> None:
        self.order.append("overlay.close")


class _NoopOverlayFactory:
    async def create(self, workspace: object) -> _NoopOverlay:
        del workspace
        return _NoopOverlay()


class _NoopExecutorFactory:
    @staticmethod
    def create(
        workspace: object,
        snapshot: object,
        deployment: object,
    ) -> object:
        del workspace, snapshot, deployment
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


def _noop_components(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    order: list[str] = []
    _TrackingLocalWorkspaceFactory.order = order
    _NoopOverlay.order = order
    monkeypatch.setattr(resources_module, "_LibraryCatalog", _NoopLibraryCatalog)
    components = replace(
        _components(tmp_path / "state.sqlite3"),
        workspace_factory=_TrackingLocalWorkspaceFactory(),
        capability_source_factory=_NoopSourceFactory(),
        capability_provider=_NoopCapabilityProvider(),
        mcp_discovery=_NoopMCPDiscovery(),
        capability_deployment=_NoopDeployment(),
        capability_overlay_factory=_NoopOverlayFactory(),
        tool_executor_factory=_NoopExecutorFactory(),
    )
    return order, components


def test_reconcile_failure_closes_opened_resources_in_reverse_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingMCPDiscovery:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def discover(
            self,
            configs: tuple[object, ...],
            source: object,
            workspace: object,
        ) -> tuple[object, ...]:
            del configs, source, workspace
            raise RuntimeError("mcp discovery exploded")

    order, components = _noop_components(tmp_path, monkeypatch)
    components = replace(components, mcp_discovery=FailingMCPDiscovery())

    with pytest.raises(RuntimeError, match="mcp discovery exploded"):
        asyncio.run(
            _reconcile_runtime_resources(
                config=WorkspaceConfig(root=tmp_path),
                components=components,
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

    order, components = _noop_components(tmp_path, monkeypatch)
    components = replace(
        components,
        capability_deployment=FailingIndexDeployment(),
    )

    with pytest.raises(RuntimeError, match="index publish exploded"):
        asyncio.run(
            _reconcile_runtime_resources(
                config=WorkspaceConfig(root=tmp_path),
                components=components,
            )
        )

    assert order == ["workspace_open", "overlay.close", "workspace.close"]


def test_project_instruction_load_failure_closes_opened_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order, components = _noop_components(tmp_path, monkeypatch)

    class _RaisingSnapshotProvider(_NoopCapabilityProvider):
        async def discover(
            self,
            source: object,
            *,
            mcp_discovery: object,
            mcp_environment: object,
            project_instructions: object,
        ) -> _NoopSnapshot:
            del source, mcp_discovery, mcp_environment, project_instructions
            raise ValueError("AGENTS.md is not valid UTF-8")

    components = replace(
        components,
        capability_provider=_RaisingSnapshotProvider(),
    )

    with pytest.raises(ValueError, match="not valid UTF-8"):
        asyncio.run(
            _reconcile_runtime_resources(
                config=WorkspaceConfig(root=tmp_path),
                components=components,
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

    order, components = _noop_components(tmp_path, monkeypatch)
    components = replace(components, workspace_factory=FailingFactory())

    with pytest.raises(ValueError, match="backend constraint failed"):
        asyncio.run(
            _reconcile_runtime_resources(
                config=WorkspaceConfig(root=tmp_path),
                components=components,
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

        async def flush(self) -> None:
            await self.backend.flush()  # type: ignore[attr-defined]

        @staticmethod
        async def close() -> None:
            order.append("workspace.close")

    class FakeOverlay:
        @staticmethod
        async def close() -> None:
            order.append("overlay.close")

    class FakeSessionStore:
        @staticmethod
        def close() -> None:
            order.append("session_store.close")

    backend = FakeBackend()
    resources = _RuntimeResources(
        workspace=FakeWorkspace(backend),  # type: ignore[arg-type]
        capabilities=CapabilityBinding(
            snapshot=_fake_snapshot(FakeLibraryCatalog()),  # type: ignore[arg-type]
            tool_executor=object(),  # type: ignore[arg-type]
            overlay=FakeOverlay(),  # type: ignore[arg-type]
        ),
        session_store=FakeSessionStore(),  # type: ignore[arg-type]
    )

    asyncio.run(resources.close())

    assert order == [
        "library.close",
        "overlay.close",
        "flush",
        "workspace.close",
        "session_store.close",
    ]


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

        async def flush(self) -> None:
            await self.backend.flush()  # type: ignore[attr-defined]

        async def close(self) -> None:
            await self.backend.close()  # type: ignore[attr-defined]

    class FakeOverlay:
        @staticmethod
        async def close() -> None:
            return None

    class FakeSessionStore:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    backend = FailingBackend()
    session_store = FakeSessionStore()
    resources = _RuntimeResources(
        workspace=FakeWorkspace(backend),  # type: ignore[arg-type]
        capabilities=CapabilityBinding(
            snapshot=_fake_snapshot(FakeLibraryCatalog()),  # type: ignore[arg-type]
            tool_executor=object(),  # type: ignore[arg-type]
            overlay=FakeOverlay(),  # type: ignore[arg-type]
        ),
        session_store=session_store,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="flush exploded"):
        asyncio.run(resources.close())

    assert backend.closed
    assert session_store.closed


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
