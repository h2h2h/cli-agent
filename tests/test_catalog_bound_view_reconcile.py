"""Bound Capability View full Tool/Skill reconcile without Host mirror.

RFC-0012 issue 06: Tool and Skill Catalogs must discover, validate and
project using only the Bound Capability View and the Workspace Filesystem.
These tests run a complete reconcile against an in-memory Bound View and an
in-memory Filesystem — no symlink, copy-up, whiteout file or Host ``Path``
is involved.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

from cli_agent.runtime._backend import (
    _CapabilityInspection,
    _DirectoryEntry,
    _FileMetadata,
    _FilesystemError,
    _FileWriteRequest,
)
from cli_agent.runtime._capability.projections import (
    write_skill_index,
    write_tool_index,
)
from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.source_view import _LogicalCapabilityView
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog

_LOWER = {
    "tools/math.py": b'"""Add two numbers."""\nPARALLEL_SAFE = True\n\ndef add(a, b):\n    return a + b\n',
    "tools/broken.py": b"def broken(:\n",
    "skills/review/SKILL.md": (
        b"---\nname: review\ndescription: Review code.\n---\nBody.\n"
    ),
    "skills/hidden/SKILL.md": (b"---\nname: hidden\ndescription: Hidden.\n---\n"),
}

_UPPER = {
    "tools/math.py": b'"""Override implementation."""\nPARALLEL_SAFE = False\n\ndef add(a, b):\n    return a * b\n',
    "tools/local.py": b'"""Workspace-only Tool."""\nVALUE = 7\n',
    "tools/math.md": b"Companion documentation for the override.\n",
    "skills/review/SKILL.md": (
        b"---\nname: review\ndescription: Overridden review.\n---\n"
    ),
}

_WHITEOUTS = frozenset({"skills/hidden/SKILL.md"})

VOLUME = "/workspace"


class _InMemoryCapabilityView:
    """Effective Bound Capability View fake with no Host mechanics.

    Provenance derives from membership in the lower/upper dictionaries;
    whiteouts are a plain set. Listing exposes the effective (merged)
    children, exactly like a materialized overlay would.
    """

    root = "/workspace"

    def __init__(
        self,
        lower: dict[str, bytes],
        upper: dict[str, bytes],
        whiteouts: frozenset[str],
    ) -> None:
        self._lower = dict(lower)
        self._upper = dict(upper)
        self._whiteouts = whiteouts

    async def inspect(self, relative_path: str) -> _CapabilityInspection:
        if relative_path in self._whiteouts:
            provenance: str | None = "whiteout"
        elif relative_path in self._upper:
            provenance = "workspace"
        elif relative_path in self._lower:
            provenance = "repertoire"
        else:
            provenance = None
        return _CapabilityInspection(
            relative_path=relative_path,
            provenance=provenance,
            shadows_repertoire=provenance == "workspace"
            and relative_path in self._lower,
            valid=True,
            validation_error=None,
        )

    async def list(self, relative_path: str) -> tuple[_DirectoryEntry, ...]:
        prefix = relative_path.rstrip("/") + "/"
        children: set[str] = set()
        for name in (*self._lower, *self._upper):
            if name in self._whiteouts:
                continue
            if not name.startswith(prefix):
                continue
            remainder = name[len(prefix) :]
            first = remainder.split("/", 1)[0]
            children.add((prefix + first, first, "/" in remainder))
        entries: list[_DirectoryEntry] = []
        for full, leaf, nested in sorted(children):
            if nested:
                entries.append(
                    _DirectoryEntry(
                        name=leaf,
                        metadata=_FileMetadata(
                            kind="directory", size=0, mtime_ns=0, mode=0o700
                        ),
                    )
                )
            else:
                entries.append(
                    _DirectoryEntry(name=leaf, metadata=await self.stat(full))
                )
        return tuple(entries)

    async def read(self, relative_path: str) -> bytes:
        try:
            return self._effective()[relative_path]
        except KeyError:
            raise _FilesystemError(
                "not_found", f"no such file: {relative_path}"
            ) from None

    async def stat(self, relative_path: str) -> _FileMetadata:
        try:
            content = self._effective()[relative_path]
        except KeyError:
            raise _FilesystemError(
                "not_found", f"no such file: {relative_path}"
            ) from None
        return _FileMetadata(
            kind="file",
            size=len(content),
            mtime_ns=0,
            mode=0o644,
        )

    def _effective(self) -> dict[str, bytes]:
        merged = dict(self._lower)
        merged.update(self._upper)
        for whiteout in self._whiteouts:
            merged.pop(whiteout, None)
        return merged


class _InMemoryFilesystem:
    """Minimal in-memory Workspace Filesystem capturing projection writes."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def resolve(self, path: str, cwd: str) -> object:
        del path, cwd
        raise AssertionError("catalogs must not resolve Host paths")

    async def write(self, request: _FileWriteRequest) -> object:
        self.files[request.path] = request.content
        return request

    async def read(self, path: str) -> bytes:
        return self.files[path]

    async def stat(self, path: str) -> _FileMetadata:
        return _FileMetadata(
            kind="file", size=len(self.files[path]), mtime_ns=0, mode=0o644
        )


def test_tool_catalog_reconciles_against_in_memory_bound_view() -> None:
    view = _InMemoryCapabilityView(_LOWER, _UPPER, _WHITEOUTS)
    filesystem = _InMemoryFilesystem()

    async def scenario() -> None:
        catalog = await _reconcile_tools(view, filesystem)

        assert isinstance(view, _LogicalCapabilityView)
        math = catalog.get("math")
        assert math is not None
        assert math.path == "tools/math.py"
        assert math.provenance == "workspace"
        assert math.shadows_repertoire is True
        assert math.valid is True
        assert math.parallel_safe is False
        assert math.documentation.startswith("Companion documentation")

        local = catalog.get("local")
        assert local is not None
        assert local.provenance == "workspace"
        assert local.shadows_repertoire is False
        assert local.valid is True
        assert local.parallel_safe is True

        broken = catalog.get("broken")
        assert broken is not None
        assert broken.provenance == "repertoire"
        assert broken.valid is False
        assert broken.validation_error is not None
        assert "Python syntax error" in (broken.validation_error or "")

        index = filesystem.files["/workspace/tools/index.md"].decode("utf-8")
        assert "| math | valid | workspace | yes | no |" in index
        assert "| broken | invalid: Python syntax error" in index

    asyncio.run(scenario())


def test_skill_catalog_reconciles_against_in_memory_bound_view() -> None:
    view = _InMemoryCapabilityView(_LOWER, _UPPER, _WHITEOUTS)
    filesystem = _InMemoryFilesystem()

    async def scenario() -> None:
        catalog = await _reconcile_skills(view, filesystem)

        review = catalog.get("review")
        assert review is not None
        assert review.path == "skills/review"
        assert review.skill_md == "skills/review/SKILL.md"
        assert review.provenance == "workspace"
        assert review.shadows_repertoire is True
        assert review.valid is True
        assert review.description == "Overridden review."

        assert catalog.get("hidden") is None

        index = filesystem.files["/workspace/skills/index.md"].decode("utf-8")
        assert "| review | valid | workspace | yes |" in index
        assert "hidden" not in index

    asyncio.run(scenario())


def test_tool_and_skill_catalogs_never_touch_host_paths() -> None:
    sources = {
        "cli_agent.runtime._capability.tools.catalog",
        "cli_agent.runtime._capability.skills.catalog",
        "cli_agent.runtime._capability.skills.parser",
        "cli_agent.runtime._capability.tools.facts",
        "cli_agent.runtime._capability.skills.facts",
    }
    forbidden = ("pathlib", "iterdir", "read_text", "read_bytes", "_atomic_write")
    for module_name in sources:
        module = importlib.import_module(module_name)
        source = Path(module.__file__).read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, (module_name, token)


def test_catalog_entries_hold_only_logical_path_facts() -> None:
    from cli_agent.runtime._capability.skills.facts import SkillEntry
    from cli_agent.runtime._capability.tools.facts import ToolEntry

    tool = ToolEntry(
        name="math",
        path="tools/math.py",
        provenance="repertoire",
        shadows_repertoire=False,
        valid=True,
        validation_error=None,
        documentation="Docs.",
        parallel_safe=True,
    )
    skill = SkillEntry(
        name="review",
        path="skills/review",
        skill_md="skills/review/SKILL.md",
        provenance="repertoire",
        shadows_repertoire=False,
        valid=True,
        validation_error=None,
        description="Review.",
    )

    assert isinstance(tool.path, str)
    assert isinstance(skill.path, str)
    assert isinstance(skill.skill_md, str)
    assert not Path(tool.path).is_absolute()
    assert not Path(skill.path).is_absolute()


async def _reconcile_tools(view, filesystem, on_diagnostic=None):
    catalog = await _ToolCatalog.discover(view, on_diagnostic)
    await write_tool_index(volume=VOLUME, filesystem=filesystem, catalog=catalog)
    return catalog


async def _reconcile_skills(view, filesystem):
    catalog = await _SkillCatalog.discover(view)
    await write_skill_index(volume=VOLUME, filesystem=filesystem, catalog=catalog)
    return catalog
