"""Local Backend: Host-filesystem Workspace implementation.

The Local Backend is the reference RFC-0012/RFC-0014 implementation. The
Backend owns every Host side-effect for execution and filesystem I/O
(Host ``Path`` operations, subprocess creation, ambient environment merge)
while exposing only backend-neutral facts through ``_BackendWorkspace`` /
``_WorkspaceFilesystem``. The Local CapabilityDeployment owns capability
materialization: Capability View attach, MCP discovery and bindings, stub
projection, and the Tool worker environment.

The Local-internal implementation classes are re-exported here so tests
and the Runtime resource bootstrap can construct fixtures without reaching
into specific submodules.
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
from cli_agent.runtime._backend.local.view import _LocalCapabilityView

__all__ = [
    "_LocalBackend",
    "_LocalBackendWorkspace",
    "_LocalCapabilityView",
    "_LocalShellExecution",
    "_LocalWorkspaceFilesystem",
    "_ProcessExecution",
]
