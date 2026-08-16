"""Static dependency boundary tests for Backend and Runtime composition.

RFC-0012 issue 01 requires the Backend contracts to stay independent of the
Session Kernel, Router, Scheduler, and the model-visible protocol, so later
migrations never couple Execution or Filesystem mechanics to those layers.
"""

import ast
import importlib
from pathlib import Path
from typing import get_type_hints

from cli_agent.runtime._backend import Backend
from cli_agent.runtime._execution import ExecutionHandle

_BACKEND_PACKAGE = "cli_agent.runtime._backend"
_FORBIDDEN_MODULES = frozenset(
    {
        "cli_agent._adapters",
        "cli_agent._workspaces",
        "cli_agent.runtime._environment.kernel",
        "cli_agent.runtime._environment.router",
        "cli_agent.runtime._environment.scheduler",
        "cli_agent.runtime._environment.protocol",
        "cli_agent.runtime._syscalls",
        "cli_agent.runtime.model",
        "cli_agent.runtime.runtime",
        "cli_agent.runtime._agent_loop",
        "cli_agent.runtime._database.state",
        "cli_agent.runtime._capability.view",
        "cli_agent.runtime._capability.deployment",
        "cli_agent.runtime._capability.library",
        "cli_agent.runtime._capability.mcp",
        "cli_agent.runtime._capability.provider",
        "cli_agent.runtime._capability.skills",
        "cli_agent.runtime._capability.tools",
    }
)

_FORBIDDEN_RUNTIME_CORE_MODULES = frozenset(
    {
        "cli_agent._adapters",
        "cli_agent._workspaces",
        "cli_agent.runtime._backend.docker",
        "cli_agent.runtime._backend.local",
    }
)


def _backend_source_files() -> tuple[Path, ...]:
    package = Path(importlib.import_module(_BACKEND_PACKAGE).__file__).parent
    return tuple(sorted(package.rglob("*.py")))


def _runtime_core_source_files() -> tuple[Path, ...]:
    package = Path(importlib.import_module("cli_agent.runtime").__file__).parent
    return tuple(sorted(package.glob("*.py")))


def test_backend_does_not_import_kernel_router_scheduler_or_model_protocol() -> None:
    for source_file in _backend_source_files():
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                assert not _imports_forbidden(imported), (source_file.name, imported)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not _imports_forbidden(alias.name), (
                        source_file.name,
                        alias.name,
                    )


def test_backend_reuses_the_execution_handle_contract() -> None:
    annotations = get_type_hints(Backend.prepare_shell)
    assert annotations["return"] is ExecutionHandle
    assert ExecutionHandle.__module__ == "cli_agent.runtime._execution"


def test_runtime_core_does_not_select_concrete_workspace_or_adapters() -> None:
    for source_file in _runtime_core_source_files():
        for imported in _imports(source_file):
            assert not _matches_any(imported, _FORBIDDEN_RUNTIME_CORE_MODULES), (
                source_file.name,
                imported,
            )


def test_provider_and_deployment_depend_on_snapshot_not_each_other() -> None:
    capability = Path(
        importlib.import_module("cli_agent.runtime._capability").__file__
    ).parent
    provider_imports = _imports(capability / "provider.py")
    deployment_imports = _imports(capability / "deployment.py")

    assert "cli_agent.runtime._capability.snapshot" in provider_imports
    assert "cli_agent.runtime._capability.snapshot" in deployment_imports
    assert "cli_agent.runtime._capability.deployment" not in provider_imports
    assert "cli_agent.runtime._capability.provider" not in deployment_imports


def _imports_forbidden(name: str) -> bool:
    return _matches_any(name, _FORBIDDEN_MODULES)


def _matches_any(name: str, forbidden_modules: frozenset[str]) -> bool:
    return any(
        name == forbidden or name.startswith(forbidden + ".")
        for forbidden in forbidden_modules
    )


def _imports(source_file: Path) -> tuple[str, ...]:
    imported: list[str] = []
    tree = ast.parse(source_file.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
        elif isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
    return tuple(imported)
