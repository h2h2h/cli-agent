import asyncio
import inspect
from collections.abc import Mapping
from pathlib import Path

import pytest

import cli_agent.runtime._resources as resources_module
from cli_agent.runtime._backend import _BackendWorkspace, _BoundCapabilityView
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.environment import _ToolEnvironment
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
        assert resources.workspace == workspace.resolve()
        assert isinstance(resources.backend, _BackendWorkspace)
        assert resources.backend.root == str(workspace.resolve())
        assert isinstance(resources.base_env, Mapping)
        assert dict(resources.base_env) == {"TOKEN": "secret"}
        assert isinstance(resources.capability_view, _BoundCapabilityView)
        assert isinstance(resources.tool_catalog, _ToolCatalog)
        assert isinstance(resources.tool_environment, _ToolEnvironment)
        assert isinstance(resources.skill_catalog, _SkillCatalog)
        assert isinstance(resources.library_catalog, _LibraryCatalog)

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
        capabilities = object()

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

    class _FakeMCPCatalog:
        @staticmethod
        async def reconcile(
            capability_view: object,
            *,
            on_diagnostic: object = None,
        ) -> None:
            del capability_view, on_diagnostic
            order.append("mcp")
            return None

    class _FakeToolCatalog:
        @staticmethod
        async def reconcile(
            capability_view: object,
            on_diagnostic: object = None,
        ) -> object:
            del capability_view, on_diagnostic
            order.append("tool_catalog")
            return object()

    class _FakeToolEnvironment:
        @staticmethod
        async def reconcile(capability_view: object) -> object:
            del capability_view
            order.append("tool_environment")
            return object()

    class _FakeSkillCatalog:
        @staticmethod
        async def reconcile(capability_view: object) -> object:
            del capability_view
            order.append("skill_catalog")
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

    class _FakeLibraryCatalog:
        @staticmethod
        async def reconcile(
            capability_view: object,
            summary_cache: object,
            capability_source: object,
        ) -> object:
            del capability_view, summary_cache, capability_source
            order.append("library_catalog")
            return object()

    def prepare_workspace(workspace: object) -> _FakePaths:
        del workspace
        order.append("prepare_workspace")
        return _FakePaths()

    def prepare_capability_source(
        repertoire: object,
        state_root: object,
    ) -> _FakeCapabilitySource:
        del repertoire, state_root
        order.append("prepare_capability_source")
        return _FakeCapabilitySource()

    monkeypatch.setattr(resources_module, "_prepare_workspace", prepare_workspace)
    monkeypatch.setattr(
        resources_module,
        "_prepare_capability_source",
        prepare_capability_source,
    )
    monkeypatch.setattr(resources_module, "_LocalBackend", _FakeLocalBackend)
    monkeypatch.setattr(resources_module, "_StateDatabase", _FakeStateDatabase)
    monkeypatch.setattr(resources_module, "_SummaryCache", _FakeSummaryCache)
    monkeypatch.setattr(resources_module, "_MCPCatalog", _FakeMCPCatalog)
    monkeypatch.setattr(resources_module, "_ToolCatalog", _FakeToolCatalog)
    monkeypatch.setattr(resources_module, "_ToolEnvironment", _FakeToolEnvironment)
    monkeypatch.setattr(resources_module, "_SkillCatalog", _FakeSkillCatalog)
    monkeypatch.setattr(resources_module, "_LibraryCatalog", _FakeLibraryCatalog)

    async def scenario() -> None:
        resources = await _reconcile_runtime_resources(
            workspace=tmp_path,
            repertoire=None,
            on_diagnostic=None,
        )

        assert order == [
            "prepare_workspace",
            "prepare_capability_source",
            "backend_open",
            "state_database",
            "summary_cache",
            "mcp",
            "tool_catalog",
            "tool_environment",
            "skill_catalog",
            "library_catalog",
        ]
        assert resources.workspace == _FakePaths.root
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
            "tool_catalog",
            "tool_environment",
            "skill_catalog",
            "library_catalog",
        }

    asyncio.run(scenario())


def test_tool_environment_fail_soft_state_enters_aggregate(
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

        assert isinstance(resources.tool_environment, _ToolEnvironment)
        assert resources.tool_environment.available is False
        assert resources.tool_environment.python is None
        assert "must be a real directory" in (resources.tool_environment.error or "")

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
