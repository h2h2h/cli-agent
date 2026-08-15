"""Capability index projections written into the Workspace (Deployment side).

The control plane never writes; these helpers materialize catalog
projections through a Backend Filesystem when the Runtime deploys a
CapabilitySnapshot.
"""

from __future__ import annotations

import posixpath

from cli_agent.runtime._backend import _FileWriteRequest, _WorkspaceFilesystem
from cli_agent.runtime._capability.provider import CapabilitySnapshot


async def write_tool_index(
    *,
    view_root: str,
    filesystem: _WorkspaceFilesystem,
    catalog,
) -> None:
    """Atomically write the Tools index projection."""

    await filesystem.write(
        _FileWriteRequest(
            path=posixpath.join(view_root, "tools/index.md"),
            content=catalog.render_index().encode("utf-8"),
        )
    )


async def write_skill_index(
    *,
    view_root: str,
    filesystem: _WorkspaceFilesystem,
    catalog,
) -> None:
    """Atomically write the Skills index projection."""

    await filesystem.write(
        _FileWriteRequest(
            path=posixpath.join(view_root, "skills/index.md"),
            content=catalog.render_index().encode("utf-8"),
        )
    )


async def write_catalog_indexes(
    *,
    view_root: str,
    filesystem: _WorkspaceFilesystem,
    snapshot: CapabilitySnapshot,
) -> None:
    """Atomically write the Tools and Skills index projections."""

    await write_tool_index(
        view_root=view_root,
        filesystem=filesystem,
        catalog=snapshot.tools,
    )
    await write_skill_index(
        view_root=view_root,
        filesystem=filesystem,
        catalog=snapshot.skills,
    )
