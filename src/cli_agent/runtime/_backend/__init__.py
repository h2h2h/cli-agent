"""Private Backend Workspace execution and filesystem domain."""

from cli_agent.runtime._backend.execution import _FilesystemExecution
from cli_agent.runtime._backend.facts import (
    _CapabilityInspection,
    _DirectoryEntry,
    _FileEdit,
    _FileEditRequest,
    _FileEditResult,
    _FileMetadata,
    _FilesystemError,
    _FileWriteRequest,
    _FileWriteResult,
    _ResolvedPath,
    _ShellExecutionRequest,
    _ToolBinding,
    _ToolExecutionRequest,
    _WorkspaceSource,
)
from cli_agent.runtime._backend.protocol import (
    Backend,
    BackendFactory,
    _WorkspaceFilesystem,
)

__all__ = [
    "BackendFactory",
    "Backend",
    "_CapabilityInspection",
    "_DirectoryEntry",
    "_FileEdit",
    "_FileEditRequest",
    "_FileEditResult",
    "_FileMetadata",
    "_FileWriteRequest",
    "_FileWriteResult",
    "_FilesystemError",
    "_FilesystemExecution",
    "_ResolvedPath",
    "_ShellExecutionRequest",
    "_ToolBinding",
    "_ToolExecutionRequest",
    "_WorkspaceFilesystem",
    "_WorkspaceSource",
]
