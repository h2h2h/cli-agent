"""CapabilityProvider: the logical capability control plane.

The Provider reads capability source (Workspace root, Repertoire lower
tree, and the Workspace state upper tree) and produces one immutable
CapabilitySnapshot aggregating Tools, Skills, MCP, and project
instructions metadata. Discovery never starts workers or processes,
never writes Workspace files, and never depends on a Backend,
BackendWorkspace, ExecutionHandle, or Tool worker; deploying the
snapshot is the Deployment plane's job.
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path

from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.mcp.config import discover_configs
from cli_agent.runtime._capability.mcp.facts import MCPServerConfig
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.source_view import (
    _LogicalCapabilityView,
    _RecordingCapabilityView,
)
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._project_instructions import (
    _load_project_instructions,
    _ProjectInstructions,
)
from cli_agent.runtime.diagnostic import RuntimeDiagnostic

CAPABILITY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Immutable logical capability facts for one discovery round.

    ``revision`` fingerprints the source content that produced the
    metadata: the same source always yields the same revision and any
    source change yields a different one. ``library`` is the live
    Deployment-owned Library Catalog attached by the Runtime through
    :meth:`with_library`; attaching it folds Library file fingerprints
    into the revision so prompt metadata and deployed facts cannot
    silently fork.
    """

    revision: str
    schema_version: int
    tools: _ToolCatalog
    skills: _SkillCatalog
    mcp_servers: tuple[MCPServerConfig, ...]
    project_instructions: _ProjectInstructions | None
    library: _LibraryCatalog | None = None

    def with_library(self, library: _LibraryCatalog) -> CapabilitySnapshot:
        """Attach the live Library Catalog and fold its facts into the revision."""

        return replace(
            self,
            library=library,
            revision=_chain_revision(
                self.revision, b"library", _library_fingerprint(library)
            ),
        )


class CapabilityProvider:
    """Discover one CapabilitySnapshot from a logical capability source."""

    def __init__(
        self,
        *,
        view: _LogicalCapabilityView,
        workspace: Path,
        instructions_loader: Callable[
            [],
            Awaitable[_ProjectInstructions | None],
        ]
        | None = None,
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> None:
        self._view = view
        self._workspace = workspace
        self._instructions_loader = instructions_loader
        self._on_diagnostic = on_diagnostic

    async def discover_mcp_configs(self) -> tuple[MCPServerConfig, ...]:
        """Discover the logical MCP server configs without side effects."""

        return await discover_configs(self._view, self._on_diagnostic)

    async def discover(
        self,
        *,
        mcp_configs: tuple[MCPServerConfig, ...] | None = None,
    ) -> CapabilitySnapshot:
        """Discover the complete snapshot without creating any resource.

        The returned snapshot covers Tools, Skills, MCP configs, and
        project instructions; the Deployment plane attaches the live
        Library Catalog afterwards through ``with_library``.

        Args:
            mcp_configs (`tuple[MCPServerConfig, ...] | None`):
                Optional pre-discovered configs; when the Deployment plane
                has already materialized MCP stubs, pass them so the Tool
                catalog reflects the deployed stubs.
        """

        view = _RecordingCapabilityView(self._view)
        if mcp_configs is None:
            mcp_configs = await discover_configs(view, self._on_diagnostic)
        tools = await _ToolCatalog.discover(view, self._on_diagnostic)
        skills = await _SkillCatalog.discover(view)
        instructions = (
            await self._instructions_loader()
            if self._instructions_loader is not None
            else _load_project_instructions(self._workspace)
        )
        inputs = view.fingerprint_inputs
        if instructions is not None:
            inputs += (
                (
                    f"AGENTS.md@{instructions.source}",
                    instructions.text.encode("utf-8"),
                ),
            )
        return CapabilitySnapshot(
            revision=_snapshot_revision(inputs),
            schema_version=CAPABILITY_SCHEMA_VERSION,
            tools=tools,
            skills=skills,
            mcp_servers=mcp_configs,
            project_instructions=instructions,
        )


def _snapshot_revision(inputs: tuple[tuple[str, bytes], ...]) -> str:
    """Hash one snapshot's source inputs into a stable revision."""

    hasher = hashlib.sha256()
    for name, content in sorted(inputs, key=lambda item: item[0]):
        hasher.update(len(name.encode("utf-8")).to_bytes(8, "big"))
        hasher.update(name.encode("utf-8"))
        hasher.update(len(content).to_bytes(8, "big"))
        hasher.update(content)
    return hasher.hexdigest()


def _chain_revision(revision: str, domain: bytes, digest: str) -> str:
    """Fold one later-discovered domain digest into an existing revision."""

    hasher = hashlib.sha256()
    hasher.update(revision.encode("utf-8"))
    hasher.update(domain)
    hasher.update(digest.encode("utf-8"))
    return hasher.hexdigest()


def _library_fingerprint(library: _LibraryCatalog) -> str:
    """Fold content-derived Library file fingerprints into one digest.

    Directory fingerprints and model-generated summaries are excluded so
    the revision depends only on source content.
    """

    hasher = hashlib.sha256()
    for entry in library.entries:
        fingerprint = entry.fingerprint
        if entry.kind != "file" or fingerprint is None:
            continue
        hasher.update(len(entry.path.encode("utf-8")).to_bytes(8, "big"))
        hasher.update(entry.path.encode("utf-8"))
        hasher.update(fingerprint.encode("utf-8"))
    return hasher.hexdigest()
