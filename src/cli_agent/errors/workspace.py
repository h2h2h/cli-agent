"""Host-facing classified errors for workspace identity."""

from __future__ import annotations

from cli_agent.errors.host import HostFacingError


class WorkspaceMismatchError(HostFacingError):
    """Raised when a durable session belongs to a different workspace.

    Sessions bind to a stable logical workspace identity, never to a
    host path; resuming across workspaces would replay an old
    conversation inside a different project and tool set, so V1 fails
    closed instead of cloning, rebinding, or migrating.

    Args:
        session_id (`str`): The session whose workspace binding differs.
        workspace_id (`str`): The workspace identity stored on the
            session.
        expected_workspace_id (`str`): The identity of the workspace the
            Runtime is currently bound to.
    """

    def __init__(
        self,
        *,
        session_id: str,
        workspace_id: str,
        expected_workspace_id: str,
    ) -> None:
        super().__init__(
            code="workspace_mismatch",
            message="Session belongs to a different workspace.",
            hint="Resume the session from its original workspace.",
            details={
                "session_id": session_id,
                "workspace_id": workspace_id,
                "expected_workspace_id": expected_workspace_id,
            },
        )
