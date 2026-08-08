"""Issue 11: static regressions fixing the RFC-0012 Backend boundary.

These source-level tests prevent Command Handlers and Capability Catalogs
from re-introducing Host ``Path`` access or subprocess shortcuts for the
live Workspace, keep the Execution contract free of Backend
discriminators, and prove the Runtime has no ``BackendSession`` or
parallel Workspace owner.
"""

import importlib
import inspect
from dataclasses import fields
from pathlib import Path

from cli_agent.runtime._backend.facts import (
    _ShellExecutionRequest,
    _ToolExecutionRequest,
)
from cli_agent.runtime._environment.execution_state import _ExecutionState
from cli_agent.runtime._resources import _RuntimeResources

_LEGACY_WORKSPACE_TOKENS = ("pathlib", "Path(")


def _package_modules(package: str) -> tuple[Path, ...]:
    package_path = Path(importlib.import_module(package).__file__).parent
    return tuple(path for path in package_path.rglob("*.py"))


def test_handlers_and_catalogs_never_use_host_path_for_the_workspace() -> None:
    packages = (
        "cli_agent.runtime._environment.handlers",
        "cli_agent.runtime._capability.tools",
        "cli_agent.runtime._capability.skills",
        "cli_agent.runtime._capability.library",
        "cli_agent.runtime._capability.mcp",
    )
    for package in packages:
        for path in _package_modules(package):
            if path.name == "__init__.py":
                continue
            if path.name == "facts.py":
                # mcp/facts.py keeps a Host-side config file loader used by
                # tests; the runtime reads descriptions through the View.
                continue
            source = path.read_text(encoding="utf-8")
            for token in _LEGACY_WORKSPACE_TOKENS:
                assert token not in source, (path, token)


def test_execution_state_and_snapshot_carry_no_backend_discriminator() -> None:
    state_fields = set(fields(_ExecutionState))
    assert state_fields.isdisjoint(
        {
            "backend",
            "provider",
            "workspace_path",
            "sandbox",
            "transport",
            "container",
        }
    )

    for fact in (_ShellExecutionRequest, _ToolExecutionRequest):
        assert "backend" not in {field.name for field in fields(fact)}

    protocol_source = Path(
        importlib.import_module("cli_agent.runtime._environment.protocol").__file__
    ).read_text(encoding="utf-8")
    assert "backend" not in protocol_source
    assert "sandbox" not in protocol_source


def test_runtime_has_no_backend_session_or_parallel_workspace_owner() -> None:
    for path in _package_modules("cli_agent.runtime"):
        source = path.read_text(encoding="utf-8")
        assert "BackendSession" not in source, path
        assert "backend_session" not in source, path

    resource_fields = {field.name for field in fields(_RuntimeResources)}
    assert "backend" in resource_fields
    assert sum(1 for field in fields(_RuntimeResources) if field.name == "backend") == 1

    runtime_source = Path(
        importlib.import_module("cli_agent.runtime.runtime").__file__
    ).read_text(encoding="utf-8")
    assert runtime_source.count("backend=") == 1


def test_environment_kernel_never_touches_local_backend_mechanics() -> None:
    from cli_agent.runtime._environment.kernel import EnvironmentKernel

    parameters = inspect.signature(EnvironmentKernel.__init__).parameters
    assert "venv" not in parameters
    assert "worker" not in parameters
    assert "tool_runtime" not in parameters

    kernel_path = Path(importlib.import_module(EnvironmentKernel.__module__).__file__)
    kernel_source = kernel_path.read_text(encoding="utf-8")
    assert "create_subprocess" not in kernel_source
    assert "os.environ" not in kernel_source
