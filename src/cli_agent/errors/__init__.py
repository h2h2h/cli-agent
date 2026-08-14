"""Consumer-facing error boundaries for the cli-agent Runtime.

Failures are classified by the consumer that can handle them, never by
the module where they originate:

- **Expected failure is data.** A normal non-zero exit code, a missing
  grep match, a policy deny, or a killed execution is an ordinary
  execution state or ``ToolResult``, never an exception.
- **Model-recoverable failures** raise `ModelFacingError`. The
  EnvironmentKernel boundary translates them into ``ToolResult.error``
  payloads; they never escape the Runtime.
- **Model-unrecoverable failures** raise `HostFacingError`. They cross
  the AgentRuntime boundary to the Host (CLI, TUI, or Server), which owns
  presentation; they never enter model context.
- **Unknown failures are diagnosed, not exposed.** `error_boundary`
  captures unexpected exceptions at public Kernel and Runtime
  boundaries: the full detail is emitted as an ``error.internal``
  Diagnostic and a sanitized `InternalRuntimeError` reaches the Host.

Exceptions are control flow while Diagnostics are observability; neither
substitutes for the other. Low-level infrastructure errors (``OSError``,
``sqlite3.Error``, provider SDK errors) stay audience-neutral inside
their modules: boundaries translate them into the audience error that
matches the current call context, because the same infrastructure
failure may be model-recoverable inside a tool call and
host-recoverable during Runtime startup.
"""

from __future__ import annotations

from cli_agent.errors.base import CliAgentError
from cli_agent.errors.boundary import (
    INTERNAL_ERROR_DIAGNOSTIC_KIND,
    DiagnosticSink,
    error_boundary,
    internal_from_exception,
)
from cli_agent.errors.host import HostFacingError, InternalRuntimeError
from cli_agent.errors.model import ModelFacingError
from cli_agent.errors.session import (
    SessionConflictError,
    SessionCorruptedError,
    SessionNotFoundError,
    SessionPersistenceError,
)

__all__ = (
    "CliAgentError",
    "DiagnosticSink",
    "HostFacingError",
    "INTERNAL_ERROR_DIAGNOSTIC_KIND",
    "InternalRuntimeError",
    "ModelFacingError",
    "SessionConflictError",
    "SessionCorruptedError",
    "SessionNotFoundError",
    "SessionPersistenceError",
    "error_boundary",
    "internal_from_exception",
)
