"""Immutable capability facts shared by discovery and deployment planes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace

from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.mcp.facts import MCPServerConfig, _MCPServerFacts
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._project_instructions import _ProjectInstructions

CAPABILITY_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    """Immutable logical capability facts for one discovery round.

    ``revision`` fingerprints the source content that produced the metadata.
    ``library`` is attached after its live catalog is reconciled; attaching it
    folds Library file fingerprints into the same revision consumed by the
    deployment plane.
    """

    revision: str
    schema_version: int
    tools: _ToolCatalog
    skills: _SkillCatalog
    mcp_servers: tuple[MCPServerConfig, ...]
    project_instructions: _ProjectInstructions | None
    mcp_facts: tuple[_MCPServerFacts, ...] = ()
    library: _LibraryCatalog | None = None

    def with_library(self, library: _LibraryCatalog) -> CapabilitySnapshot:
        """Attach the live Library Catalog and fold its facts into the revision."""

        return replace(
            self,
            library=library,
            revision=_chain_revision(
                self.revision,
                b"library",
                _library_fingerprint(library),
            ),
        )


def _chain_revision(revision: str, domain: bytes, digest: str) -> str:
    hasher = hashlib.sha256()
    hasher.update(revision.encode("utf-8"))
    hasher.update(domain)
    hasher.update(digest.encode("utf-8"))
    return hasher.hexdigest()


def _library_fingerprint(library: _LibraryCatalog) -> str:
    """Fold source-derived Library file fingerprints into one digest."""

    hasher = hashlib.sha256()
    for entry in library.entries:
        fingerprint = entry.fingerprint
        if entry.kind != "file" or fingerprint is None:
            continue
        hasher.update(len(entry.path.encode("utf-8")).to_bytes(8, "big"))
        hasher.update(entry.path.encode("utf-8"))
        hasher.update(fingerprint.encode("utf-8"))
    return hasher.hexdigest()
