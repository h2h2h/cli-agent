"""Static dependency boundary tests for the private Backend domain.

RFC-0012 issue 01 requires the Backend contracts to stay independent of the
Session Kernel, Router, Scheduler, and the model-visible protocol, so later
migrations never couple Execution or Filesystem mechanics to those layers.
"""

import ast
import importlib
from pathlib import Path
from typing import get_type_hints

from cli_agent.runtime._backend import _BackendWorkspace
from cli_agent.runtime._execution import ExecutionHandle

_BACKEND_PACKAGE = "cli_agent.runtime._backend"
_FORBIDDEN_MODULES = frozenset(
    {
        "cli_agent.runtime._environment.kernel",
        "cli_agent.runtime._environment.routing",
        "cli_agent.runtime._environment.scheduler",
        "cli_agent.runtime._environment.protocol",
        "cli_agent.runtime._syscalls",
        "cli_agent.runtime.model",
        "cli_agent.runtime.runtime",
        "cli_agent.runtime._agent_loop",
        "cli_agent.runtime._database.state",
        "cli_agent.runtime._capability.view",
    }
)


def _backend_source_files() -> tuple[Path, ...]:
    package = Path(importlib.import_module(_BACKEND_PACKAGE).__file__).parent
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
    annotations = get_type_hints(_BackendWorkspace.prepare_shell)
    assert annotations["return"] is ExecutionHandle
    assert ExecutionHandle.__module__ == "cli_agent.runtime._execution"


def _imports_forbidden(name: str) -> bool:
    return any(
        name == forbidden or name.startswith(forbidden + ".")
        for forbidden in _FORBIDDEN_MODULES
    )
