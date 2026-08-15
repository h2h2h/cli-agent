"""Runtime resource aggregate reconciliation, rollback, and close tests."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.runtime._resources as resources_module
import cli_agent.runtime._workspace as workspace_module
from cli_agent.runtime._backend import _BackendWorkspace, _BoundCapabilityView
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.provider import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilitySnapshot,
)
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
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
        assert isinstance(resources.capability_view, _BoundCapabilityView)
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

    class _FakeCapabilitySource:
        repertoire = tmp_path / "repertoire"

    class _FakeBackendWorkspace:
        workspace_environment = {"TOKEN": "secret"}
        capabilities = SimpleNamespace(root="fake-root")
        filesystem = object()

        @staticmethod
        async def reconcile_tool_runtime() -> object:
            order.append("tool_runtime")
            return object()

        @staticmethod
        async def flush() -> None:
            return None

        @staticmethod
        async def close() -> None:
            return None

    class _FakeLocalBackend:
        @staticmethod
        async def open_workspace(
            source: object,
            capability_source: object,
            capability_state: object,
        ) -> object:
            del source, capability_source, capability_state
            order.append("backend_open")
            return _FakeBackendWorkspace()

    class _FakeSnapshot:
        def __init__(self, library: object | None) -> None:
            self.library = library
            self.tools = object()
            self.skills = object()

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

    class _FakeMCPCatalog:
        @staticmethod
        async def reconcile(
            backend: object,
            on_diagnostic: object = None,
            *,
            configs: object = None,
        ) -> None:
            del backend, on_diagnostic, configs
            order.append("mcp")
            return None

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

    def prepare_workspace(workspace: object) -> _FakePaths:
        del workspace
        order.append("prepare_workspace")
        return _FakePaths()

    def load_workspace_identity(state: object) -> str:
        del state
        order.append("workspace_identity")
        return "local:00000000000000000000000000000000"

    def prepare_capability_source(
        repertoire: object,
        state_root: object,
    ) -> _FakeCapabilitySource:
        del repertoire, state_root
        order.append("prepare_capability_source")
        return _FakeCapabilitySource()

    monkeypatch.setattr(workspace_module, "_prepare_workspace", prepare_workspace)
    monkeypatch.setattr(
        workspace_module,
        "_load_workspace_identity",
        load_workspace_identity,
    )
    monkeypatch.setattr(
        workspace_module,
        "_prepare_capability_source",
        prepare_capability_source,
    )
    monkeypatch.setattr(workspace_module, "_LocalBackend", _FakeLocalBackend)
    monkeypatch.setattr(
        resources_module,
        "CapabilityProvider",
        _FakeCapabilityProvider,
    )
    monkeypatch.setattr(resources_module, "_StateDatabase", _FakeStateDatabase)
    monkeypatch.setattr(resources_module, "_SummaryCache", _FakeSummaryCache)
    monkeypatch.setattr(resources_module, "SessionStore", _FakeSessionStore)
    monkeypatch.setattr(resources_module, "_MCPCatalog", _FakeMCPCatalog)
    monkeypatch.setattr(resources_module, "_LibraryCatalog", _FakeLibraryCatalog)

    async def write_indexes(**kwargs: object) -> None:
        del kwargs
        order.append("write_indexes")

    monkeypatch.setattr(resources_module, "write_catalog_indexes", write_indexes)

    async def scenario() -> None:
        resources = await _reconcile_runtime_resources(
            workspace=tmp_path,
            repertoire=None,
            on_diagnostic=None,
        )

        assert order == [
            "prepare_workspace",
            "workspace_identity",
            "prepare_capability_source",
            "backend_open",
            "mcp_configs",
            "mcp",
            "snapshot",
            "write_indexes",
            "tool_runtime",
            "state_database",
            "summary_cache",
            "session_store",
            "library_catalog",
        ]
        assert resources.workspace.root == str(_FakePaths.root)
        assert dict(resources.base_env) == {"TOKEN": "secret"}

    asyncio.run(scenario())


def test_mcp_projection_result_is_not_retained_in_aggregate(
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
            "session_store",
        }

    asyncio.run(scenario())


def test_tool_runtime_fail_soft_state_does_not_break_runtime_open(
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

        status = await resources.backend.reconcile_tool_runtime()
        assert status.available is False
        assert "must be a real directory" in (status.error or "")

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
    """Fake Backend Workspace recording open/close lifecycle events."""

    def __init__(
        self, order: list[str], *, tool_runtime_failure: Exception | None = None
    ) -> None:
        self.order = order
        self.workspace_environment = {"TOKEN": "secret"}
        self.capabilities = SimpleNamespace(root="fake-root")
        self.filesystem = object()
        self._tool_runtime_failure = tool_runtime_failure

    async def reconcile_tool_runtime(self) -> object:
        self.order.append("tool_runtime")
        if self._tool_runtime_failure is not None:
            raise self._tool_runtime_failure
        return object()

    async def flush(self) -> None:
        self.order.append("flush")

    async def close(self) -> None:
        self.order.append("backend.close")


class _TrackingLocalBackend:
    """Fake Backend recording open and Tool Runtime events on a shared list."""

    order: list[str] = []
    tool_runtime_failure: Exception | None = None

    async def open_workspace(
        self,
        source: object,
        capability_source: object,
        capability_state: object,
    ) -> _TrackingWorkspace:
        del source, capability_source, capability_state
        self.order.append("backend_open")
        return _TrackingWorkspace(
            self.order,
            tool_runtime_failure=self.tool_runtime_failure,
        )


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


class _NoopMCPCatalog:
    @staticmethod
    async def reconcile(
        backend: object,
        on_diagnostic: object = None,
        *,
        configs: object = None,
    ) -> None:
        del backend, on_diagnostic, configs
        return None


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
    _TrackingLocalBackend.order = order
    _TrackingLocalBackend.tool_runtime_failure = None
    _TrackingStateDatabase.order = order
    monkeypatch.setattr(workspace_module, "_LocalBackend", _TrackingLocalBackend)
    monkeypatch.setattr(
        resources_module,
        "CapabilityProvider",
        _NoopCapabilityProvider,
    )
    monkeypatch.setattr(resources_module, "_StateDatabase", _TrackingStateDatabase)
    monkeypatch.setattr(resources_module, "_SummaryCache", _NoopSummaryCache)
    monkeypatch.setattr(resources_module, "SessionStore", _NoopSessionStore)
    monkeypatch.setattr(resources_module, "_MCPCatalog", _NoopMCPCatalog)
    monkeypatch.setattr(resources_module, "_LibraryCatalog", _NoopLibraryCatalog)

    async def write_indexes(**kwargs: object) -> None:
        del kwargs

    monkeypatch.setattr(resources_module, "write_catalog_indexes", write_indexes)
    return order


def test_reconcile_failure_closes_opened_resources_in_reverse_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingMCPCatalog:
        @staticmethod
        async def reconcile(
            backend: object,
            on_diagnostic: object = None,
            *,
            configs: object = None,
        ) -> None:
            del backend, on_diagnostic, configs
            raise RuntimeError("mcp discovery exploded")

    order = _install_noop_reconcile_fakes(monkeypatch)
    monkeypatch.setattr(resources_module, "_MCPCatalog", FailingMCPCatalog)

    with pytest.raises(RuntimeError, match="mcp discovery exploded"):
        asyncio.run(
            _reconcile_runtime_resources(
                workspace=tmp_path,
                repertoire=None,
                on_diagnostic=None,
            )
        )

    assert order == ["backend_open", "backend.close"]


def test_reconcile_failure_at_tool_runtime_closes_opened_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _install_noop_reconcile_fakes(monkeypatch)
    _TrackingLocalBackend.tool_runtime_failure = RuntimeError("tool sync exploded")

    with pytest.raises(RuntimeError, match="tool sync exploded"):
        asyncio.run(
            _reconcile_runtime_resources(
                workspace=tmp_path,
                repertoire=None,
                on_diagnostic=None,
            )
        )

    assert order == [
        "backend_open",
        "tool_runtime",
        "backend.close",
    ]


def test_project_instruction_load_failure_closes_opened_resources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order = _install_noop_reconcile_fakes(monkeypatch)
    monkeypatch.setattr(
        resources_module,
        "_LibraryCatalog",
        _NoopLibraryCatalog,
    )

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

    assert order == ["backend_open", "backend.close"]


def test_backend_open_failure_propagates_without_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class FailingLocalBackend:
        async def open_workspace(
            self,
            source: object,
            capability_source: object,
            capability_state: object,
        ) -> object:
            del source, capability_source, capability_state
            nonlocal calls
            calls += 1
            raise ValueError("backend constraint failed")

    order = _install_noop_reconcile_fakes(monkeypatch)
    monkeypatch.setattr(workspace_module, "_LocalBackend", FailingLocalBackend)

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
        session_store=object(),
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
        session_store=object(),
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
