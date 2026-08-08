"""Local Backend: Host-filesystem Workspace implementation.

The Local Backend is the reference RFC-0012 Backend. It owns every Host
side-effect (Host ``Path`` operations, subprocess creation, ambient
environment merge, file-level Capability View materialization, Workspace
Tool Runtime, Workspace MCP Runtime) while exposing only backend-neutral
facts through ``_BackendWorkspace`` / ``_WorkspaceFilesystem`` /
``_BoundCapabilityView`` / ``_WorkspaceMCPRuntime``.

The Local-internal implementation classes are re-exported here so tests and
the Runtime resource bootstrap can construct fixtures without reaching into
specific submodules.
"""

from cli_agent.runtime._backend.local.backend import (
    _LocalBackend,
    _LocalBackendWorkspace,
)
from cli_agent.runtime._backend.local.filesystem import _LocalWorkspaceFilesystem
from cli_agent.runtime._backend.local.shell import (
    _LocalShellExecution,
    _ProcessExecution,
)
from cli_agent.runtime._backend.local.view import (
    _LocalCapabilityView,
    _UnimplementedCapabilityView,
)

__all__ = [
    "_LocalBackend",
    "_LocalBackendWorkspace",
    "_LocalCapabilityView",
    "_LocalShellExecution",
    "_LocalWorkspaceFilesystem",
    "_ProcessExecution",
    "_UnimplementedCapabilityView",
]
