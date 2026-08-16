"""Docker Backend: containerized Workspace execution and volume filesystem.

The Docker Backend is the second RFC-0012 implementation: it owns the
persistent Workspace volume, one long-lived filesystem helper container,
and one ephemeral execution container per Shell ``run()``, while exposing
only backend-neutral facts through ``Backend`` /
``_WorkspaceFilesystem``. Capability materialization and Tool execution
belong to the CapabilityDeployment plane (RFC-0017).

The Docker-internal implementation classes are re-exported here so tests
and Workspace factories can construct fixtures without reaching into
specific submodules.
"""

from cli_agent.runtime._backend.docker.backend import (
    _DockerBackend,
    _DockerBackendWorkspace,
    _DockerWorkspaceSource,
)
from cli_agent.runtime._backend.docker.execution import _DockerShellExecution
from cli_agent.runtime._backend.docker.filesystem import _DockerWorkspaceFilesystem

__all__ = [
    "_DockerBackend",
    "_DockerBackendWorkspace",
    "_DockerShellExecution",
    "_DockerWorkspaceFilesystem",
    "_DockerWorkspaceSource",
]
