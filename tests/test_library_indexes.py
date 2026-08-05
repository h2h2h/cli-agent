import asyncio
from pathlib import Path

import pytest

import cli_agent.runtime._capability.library.catalog as catalog_module
from cli_agent.runtime._capability.library.cache import _SummaryCache
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.library.facts import (
    _content_digest,
    _file_fingerprint,
)
from cli_agent.runtime._capability.view import _CapabilityView
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._state_db import _StateDatabase


def _repertoire(workspace: Path) -> Path:
    repertoire = workspace.parent / f"{workspace.name}-repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    return repertoire


def _cache(workspace: Path) -> _SummaryCache:
    return _SummaryCache(_StateDatabase.open(workspace / "state.sqlite3"))


def _fingerprint_of(content: str) -> str:
    return _file_fingerprint(_content_digest(content.encode("utf-8")))


def _reconcile(workspace: Path, repertoire: Path) -> _LibraryCatalog:
    async def scenario() -> _LibraryCatalog:
        _prepare_workspace(workspace)
        view = _CapabilityView.open(workspace, repertoire)
        return await _LibraryCatalog.reconcile(view, _cache(workspace))

    return asyncio.run(scenario())


def _index(workspace: Path, *parts: str) -> str:
    return (workspace / ".workspace" / "library" / Path(*parts) / "index.md").read_text(
        encoding="utf-8"
    )


def test_indexes_rendered_for_root_and_every_directory(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "resources" / "architecture").mkdir(parents=True)
    (repertoire / "library" / "resources" / "architecture" / "design.md").write_text(
        "d\n", encoding="utf-8"
    )
    (repertoire / "library" / "memory").mkdir()
    (repertoire / "library" / "memory" / "notes.txt").write_text(
        "note\n", encoding="utf-8"
    )
    (repertoire / "library" / "guide.md").write_text("g\n", encoding="utf-8")

    catalog = _reconcile(tmp_path, repertoire)

    root_index = _index(tmp_path)
    assert (
        "| resources | pending | Directory summary generation pending. | [resources](./resources/index.md) |"
        in root_index
    )
    assert (
        "| memory | pending | Directory summary generation pending. | [memory](./memory/index.md) |"
        in root_index
    )
    assert (
        "| guide.md | pending | repertoire | no | Summary generation pending. | [guide.md](./guide.md) |"
        in root_index
    )
    assert "architecture" not in root_index
    assert "notes.txt" not in root_index
    assert catalog.entries != ()

    resources_index = _index(tmp_path, "resources")
    assert (
        "| architecture | pending | Directory summary generation pending. | [architecture](./architecture/index.md) |"
        in resources_index
    )
    assert "design.md" not in resources_index

    architecture_index = _index(tmp_path, "resources", "architecture")
    assert (
        "| design.md | pending | repertoire | no | Summary generation pending. | [design.md](./design.md) |"
        in architecture_index
    )

    memory_index = _index(tmp_path, "memory")
    assert (
        "| notes.txt | pending | repertoire | no | Summary generation pending. | [notes.txt](./notes.txt) |"
        in memory_index
    )


def test_root_index_frontmatter_and_entry_lines(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "memory").mkdir()
    (repertoire / "library" / "memory" / "notes.txt").write_text(
        "note\n", encoding="utf-8"
    )
    (repertoire / "library" / "guide.md").write_text("g\n", encoding="utf-8")

    _reconcile(tmp_path, repertoire)

    lines = _index(tmp_path).splitlines()
    assert lines[:6] == [
        "---",
        "name: library",
        "path: library",
        "type: dir",
        "status: pending",
        "description: Directory summary generation pending.",
    ]
    assert (
        "| memory | pending | Directory summary generation pending. | [memory](./memory/index.md) |"
        in lines
    )
    assert (
        "| guide.md | pending | repertoire | no | Summary generation pending. | "
        "[guide.md](./guide.md) |"
    ) in lines

    memory_lines = _index(tmp_path, "memory").splitlines()
    assert memory_lines[:6] == [
        "---",
        "name: memory",
        "path: library/memory",
        "type: dir",
        "status: pending",
        "description: Directory summary generation pending.",
    ]


def test_cache_hit_renders_ready_and_miss_renders_pending(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "cached.md").write_text(
        "cached content\n", encoding="utf-8"
    )
    (repertoire / "library" / "fresh.md").write_text(
        "fresh content\n", encoding="utf-8"
    )

    cache = _cache(tmp_path)
    cache.upsert(_fingerprint_of("cached content\n"), "file", "Cached summary.")
    cache.close()

    catalog = _reconcile(tmp_path, repertoire)

    assert catalog.get("cached.md").status == "ready"  # type: ignore[union-attr]
    assert catalog.get("cached.md").summary == "Cached summary."  # type: ignore[union-attr]
    assert catalog.get("fresh.md").status == "pending"  # type: ignore[union-attr]

    index = _index(tmp_path)
    assert (
        "| cached.md | ready | repertoire | no | Cached summary. | "
        "[cached.md](./cached.md) |"
    ) in index
    assert (
        "| fresh.md | pending | repertoire | no | Summary generation pending. |"
        in index
    )
    assert "Summary generation pending." in index


def test_failed_and_unsupported_entries_render_status_texts(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "broken.md").write_bytes(b"\xff\xfe")
    (repertoire / "library" / "data.bin").write_bytes(b"\x00\x01")

    _reconcile(tmp_path, repertoire)

    index = _index(tmp_path)
    assert (
        "| broken.md | failed | repertoire | no | file is not valid UTF-8 | "
        "[broken.md](./broken.md) |"
    ) in index
    assert (
        "| data.bin | unsupported | repertoire | no | Unsupported format; read the source file directly. | "
        "[data.bin](./data.bin) |"
    ) in index


def test_summaries_and_names_are_escaped(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "pipe|file.md").write_text("content\n", encoding="utf-8")

    cache = _cache(tmp_path)
    cache.upsert(
        _fingerprint_of("content\n"),
        "file",
        "Summary with | pipe\nand a newline.",
    )
    cache.close()

    _reconcile(tmp_path, repertoire)

    index = _index(tmp_path)
    assert "| pipe\\|file.md |" in index
    assert "| Summary with \\| pipe and a newline. |" in index
    assert "\nand a newline" not in index


def test_workspace_override_renders_workspace_provenance_and_shadow(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "guide.md").write_text("lower\n", encoding="utf-8")

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    view_md = view.root / "library" / "guide.md"
    view._copy_up(view_md)
    view_md.write_text("upper\n", encoding="utf-8")

    asyncio.run(_LibraryCatalog.reconcile(view, _cache(tmp_path)))

    index = _index(tmp_path)
    assert (
        "| guide.md | pending | workspace | yes | Summary generation pending. | "
        "[guide.md](./guide.md) |"
    ) in index


def test_repertoire_index_md_is_shadowed_not_modified(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "index.md").write_text(
        "user lower index\n", encoding="utf-8"
    )
    (repertoire / "library" / "design.md").write_text("d\n", encoding="utf-8")

    _reconcile(tmp_path, repertoire)

    workspace_index = tmp_path / ".workspace" / "library" / "index.md"
    assert not workspace_index.is_symlink()
    assert "design.md" in workspace_index.read_text(encoding="utf-8")
    assert (repertoire / "library" / "index.md").read_text() == "user lower index\n"


def test_generated_indexes_are_not_re_discovered(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "design.md").write_text("d\n", encoding="utf-8")

    _reconcile(tmp_path, repertoire)
    catalog = _reconcile(tmp_path, repertoire)

    assert {entry.path for entry in catalog.entries} == {"design.md"}


def test_rendered_indexes_are_reproducible(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "resources").mkdir()
    (repertoire / "library" / "resources" / "design.md").write_text(
        "d\n", encoding="utf-8"
    )
    (repertoire / "library" / "guide.md").write_text("g\n", encoding="utf-8")

    _reconcile(tmp_path, repertoire)
    first = {
        "index.md": _index(tmp_path),
        "resources/index.md": _index(tmp_path, "resources"),
    }
    _reconcile(tmp_path, repertoire)
    second = {
        "index.md": _index(tmp_path),
        "resources/index.md": _index(tmp_path, "resources"),
    }

    assert first == second


def test_indexes_write_deepest_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "resources" / "architecture").mkdir(parents=True)
    (repertoire / "library" / "resources" / "architecture" / "design.md").write_text(
        "d\n", encoding="utf-8"
    )
    (repertoire / "library" / "memory").mkdir()

    written: list[str] = []

    def record(path: Path, content: bytes) -> None:
        del content
        written.append(str(path.relative_to(tmp_path / ".workspace" / "library")))

    monkeypatch.setattr(catalog_module, "_atomic_write", record)

    _reconcile(tmp_path, repertoire)

    assert written == [
        "resources/architecture/index.md",
        "memory/index.md",
        "resources/index.md",
        "index.md",
    ]


def test_indexes_contain_no_chunks_or_hidden_metadata(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "design.md").write_text(
        "# Design\n\nSome body content.\n", encoding="utf-8"
    )

    _reconcile(tmp_path, repertoire)

    index = _index(tmp_path)
    assert "chunk" not in index
    assert "fingerprint" not in index
    assert "Some body content" not in index


def test_empty_library_still_renders_root_index(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)

    _reconcile(tmp_path, repertoire)

    index = _index(tmp_path)
    assert index.splitlines()[:6] == [
        "---",
        "name: library",
        "path: library",
        "type: dir",
        "status: ready",
        "description: Empty directory.",
    ]
    assert "## Directories" in index
    assert "## Files" in index
