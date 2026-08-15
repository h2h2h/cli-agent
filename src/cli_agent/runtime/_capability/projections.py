"""Capability index projections written into the Workspace (Deployment side).

The control plane never writes; these helpers materialize catalog
projections through a Workspace Filesystem when the CapabilityDeployment
deploys a CapabilitySnapshot. Targets are addressed with Backend-relative
capability volume paths, never Host mirror paths.
"""

from __future__ import annotations

import posixpath

from cli_agent.runtime._backend import _FileWriteRequest, _WorkspaceFilesystem
from cli_agent.runtime._capability.provider import CapabilitySnapshot


async def write_tool_index(
    *,
    volume: str,
    filesystem: _WorkspaceFilesystem,
    catalog,
) -> None:
    """Atomically write the Tools index projection."""

    await filesystem.write(
        _FileWriteRequest(
            path=posixpath.join(volume, "tools/index.md"),
            content=catalog.render_index().encode("utf-8"),
        )
    )


async def write_skill_index(
    *,
    volume: str,
    filesystem: _WorkspaceFilesystem,
    catalog,
) -> None:
    """Atomically write the Skills index projection."""

    await filesystem.write(
        _FileWriteRequest(
            path=posixpath.join(volume, "skills/index.md"),
            content=catalog.render_index().encode("utf-8"),
        )
    )


def render_catalog_indexes(
    *,
    snapshot: CapabilitySnapshot,
) -> dict[str, bytes]:
    """Return the desired Tools and Skills index artifacts."""

    return {
        "tools/index.md": snapshot.tools.render_index().encode("utf-8"),
        "skills/index.md": snapshot.skills.render_index().encode("utf-8"),
    }
