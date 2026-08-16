"""Local Backend: Host-filesystem Workspace implementation.

The Local Backend is the reference RFC-0012/RFC-0014 implementation. The
Backend owns every Host side-effect for execution and filesystem I/O
(Host ``Path`` operations, subprocess creation, ambient environment merge)
while exposing only backend-neutral facts through ``Backend`` /
``_WorkspaceFilesystem``. Capability materialization and Tool execution live
in outer adapters and are deliberately not exported by this package.

The Local-internal Backend implementation classes are re-exported here so
Workspace factories and Backend tests can construct them without reaching
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

__all__ = [
    "_LocalBackend",
    "_LocalBackendWorkspace",
    "_LocalShellExecution",
    "_LocalWorkspaceFilesystem",
    "_ProcessExecution",
]
