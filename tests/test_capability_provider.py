"""CapabilityProvider and CapabilitySnapshot control-plane tests (issue 013).

The Provider must stay independent of Backend, Workspace, and execution
packages; a snapshot must be immutable, deterministic for identical
source, unified across Tools, Skills, MCP, Library, and project
instructions; and discovery must create no worker, venv, binding, or
Workspace file.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import json
from pathlib import Path

import pytest

from cli_agent.runtime._capability.provider import (
    CAPABILITY_SCHEMA_VERSION,
    CapabilityProvider,
)
from cli_agent.runtime._capability.source_view import _CapabilitySourceView
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._project_instructions import _ProjectInstructions
from cli_agent.runtime._system_message import assemble_system_message

_FORBIDDEN_MODULES = frozenset(
    {
        "cli_agent.runtime._backend",
        "cli_agent.runtime._workspace",
        "cli_agent.runtime._execution",
    }
)


def _provider(tmp_path: Path, *, workspace: Path | None = None):
    root = workspace if workspace is not None else tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    paths = _prepare_workspace(root)
    repertoire = tmp_path / "repertoire"
    for name in ("tools", "skills", "library", "_mcp"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    (root / ".workspace" / "tools").mkdir(parents=True, exist_ok=True)
    view = _CapabilitySourceView(upper_root=paths.state, repertoire=repertoire)
    return CapabilityProvider(view=view, workspace=root), root, repertoire


def _tool(directory: Path, name: str, source: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(source, encoding="utf-8")


def _skill(path: Path, name: str, description: str) -> None:
    directory = path / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _mcp(repertoire: Path, name: str) -> None:
    directory = repertoire / "_mcp" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "config.json").write_text(
        json.dumps(
            {
                "name": name,
                "transport": "stdio",
                "command": ["echo", "server"],
            }
        ),
        encoding="utf-8",
    )


def test_provider_module_imports_no_backend_workspace_or_execution() -> None:
    package = importlib.import_module("cli_agent.runtime._capability")
    package_root = Path(package.__file__).parent
    control_plane = (
        "provider.py",
        "facts.py",
        "source_view.py",
        "tools/catalog.py",
        "tools/facts.py",
        "tools/grammar.py",
        "skills/catalog.py",
        "skills/facts.py",
        "skills/parser.py",
        "mcp/config.py",
        "mcp/facts.py",
    )
    for name in control_plane:
        path = package_root / name
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                assert not any(
                    imported == forbidden or imported.startswith(forbidden + ".")
                    for forbidden in _FORBIDDEN_MODULES
                ), (name, imported)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(
                        alias.name == forbidden
                        or alias.name.startswith(forbidden + ".")
                        for forbidden in _FORBIDDEN_MODULES
                    ), (name, alias.name)


def test_snapshot_is_immutable_and_carries_schema_version(tmp_path: Path) -> None:
    provider, _, _ = _provider(tmp_path)
    snapshot = asyncio.run(provider.discover())

    assert snapshot.schema_version == CAPABILITY_SCHEMA_VERSION
    assert snapshot.revision
    with pytest.raises(AttributeError):
        snapshot.revision = "other"  # type: ignore[misc]


def test_identical_source_yields_identical_snapshot_and_revision(
    tmp_path: Path,
) -> None:
    first, root, repertoire = _provider(tmp_path)
    _tool(repertoire / "tools", "calc", '"""Calc."""\nVALUE = 1\n')
    _skill(repertoire / "skills", "review", "Review helper.")
    _mcp(repertoire, "math")
    (root / "AGENTS.md").write_text("# rules\n", encoding="utf-8")

    first_snapshot = asyncio.run(first.discover())

    second, _, _ = _provider(tmp_path)
    second_snapshot = asyncio.run(second.discover())

    assert second_snapshot.revision == first_snapshot.revision
    first_body = "\n".join(
        block.text
        for block in assemble_system_message(
            root,
            "host",
            snapshot=first_snapshot,
        ).content
    )
    second_body = "\n".join(
        block.text
        for block in assemble_system_message(
            root,
            "host",
            snapshot=second_snapshot,
        ).content
    )
    assert first_body == second_body


def test_source_change_yields_a_different_revision(tmp_path: Path) -> None:
    provider, root, repertoire = _provider(tmp_path)
    _tool(repertoire / "tools", "calc", '"""Calc."""\nVALUE = 1\n')

    first = asyncio.run(provider.discover())
    _tool(repertoire / "tools", "calc", '"""Calc."""\nVALUE = 2\n')
    second = asyncio.run(provider.discover())

    assert first.revision != second.revision


def test_snapshot_aggregates_all_five_capability_kinds(tmp_path: Path) -> None:
    provider, root, repertoire = _provider(tmp_path)
    _tool(repertoire / "tools", "calc", '"""Calc."""\nVALUE = 1\n')
    _skill(repertoire / "skills", "review", "Review helper.")
    _mcp(repertoire, "math")
    (root / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
    (repertoire / "library" / "notes.md").write_text(
        "library notes\n", encoding="utf-8"
    )

    snapshot = asyncio.run(provider.discover())

    assert snapshot.tools.get("calc") is not None
    assert snapshot.skills.get("review") is not None
    assert [server.name for server in snapshot.mcp_servers] == ["math"]
    assert snapshot.project_instructions == _ProjectInstructions(
        source=str(root / "AGENTS.md"),
        text="# rules\n",
    )
    assert snapshot.library is None

    class _FakeLibrary:
        entries = (
            type(
                "Entry",
                (),
                {
                    "path": "notes.md",
                    "kind": "file",
                    "fingerprint": hashlib.sha256(b"library notes\n").hexdigest(),
                },
            )(),
            type(
                "Entry",
                (),
                {"path": "docs", "kind": "directory", "fingerprint": None},
            )(),
        )

    fake_library = _FakeLibrary()
    with_library = snapshot.with_library(fake_library)
    assert with_library.library is fake_library
    assert with_library.revision != snapshot.revision


def test_discovery_creates_no_files_or_processes(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    paths = _prepare_workspace(root)
    repertoire = tmp_path / "repertoire"
    (repertoire / "tools").mkdir(parents=True)
    _tool(repertoire / "tools", "calc", '"""Calc."""\nVALUE = 1\n')

    view = _CapabilitySourceView(upper_root=paths.state, repertoire=repertoire)
    provider = CapabilityProvider(view=view, workspace=root)

    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    snapshot = asyncio.run(provider.discover())
    after = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))

    assert snapshot.tools.get("calc") is not None
    assert before == after


def test_workspace_tool_shadows_repertoire_without_duplicate(tmp_path: Path) -> None:
    provider, root, repertoire = _provider(tmp_path)
    _tool(repertoire / "tools", "calc", '"""Lower."""\nLOWER = 1\n')
    _tool(root / ".workspace" / "tools", "calc", '"""Upper."""\nUPPER = 2\n')

    snapshot = asyncio.run(provider.discover())

    calc = snapshot.tools.get("calc")
    assert calc is not None
    assert calc.provenance == "workspace"
    assert calc.shadows_repertoire is True
    assert [entry.name for entry in snapshot.tools.entries].count("calc") == 1


def test_whiteout_hides_repertoire_tool(tmp_path: Path) -> None:
    provider, root, repertoire = _provider(tmp_path)
    _tool(repertoire / "tools", "calc", '"""Lower."""\nLOWER = 1\n')
    whiteout = (
        root / ".workspace" / ".capability-view" / "whiteouts" / "tools" / "calc.py"
    )
    whiteout.parent.mkdir(parents=True)
    whiteout.write_text("", encoding="utf-8")

    snapshot = asyncio.run(provider.discover())

    assert snapshot.tools.get("calc") is None


def test_malformed_mcp_config_is_reported_and_skipped(tmp_path: Path) -> None:
    provider, root, repertoire = _provider(tmp_path)
    _mcp(repertoire, "broken")
    (repertoire / "_mcp" / "broken" / "config.json").write_text(
        "{not json", encoding="utf-8"
    )
    received: list[object] = []
    provider._on_diagnostic = received.append

    snapshot = asyncio.run(provider.discover())

    assert snapshot.mcp_servers == ()
    assert received
    diagnostic = received[0]
    assert diagnostic.kind == "mcp.config_invalid"
