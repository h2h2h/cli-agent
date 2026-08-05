import asyncio
from pathlib import Path

import pytest

from cli_agent.runtime._capability.library.cache import _SummaryCache
from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
from cli_agent.runtime._capability.library.facts import (
    _content_digest,
    _directory_fingerprint,
    _file_fingerprint,
)
from cli_agent.runtime._capability.library.parser import (
    LibraryParseError,
    TextLibraryFileParser,
)
from cli_agent.runtime._capability.view import _CapabilityView
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._state_db import _StateDatabase


def _repertoire(workspace: Path) -> Path:
    repertoire = workspace.parent / f"{workspace.name}-repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    return repertoire


def _cache(tmp_path: Path) -> _SummaryCache:
    return _SummaryCache(_StateDatabase.open(tmp_path / "state.sqlite3"))


def _reconcile(workspace: Path, repertoire: Path) -> _LibraryCatalog:
    async def scenario() -> _LibraryCatalog:
        _prepare_workspace(workspace)
        view = _CapabilityView.open(workspace, repertoire)
        return await _LibraryCatalog.reconcile(view, _cache(workspace))

    return asyncio.run(scenario())


def test_catalog_discovers_effective_library_facts(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "resources").mkdir()
    (repertoire / "library" / "resources" / "design.md").write_text(
        "# Design\n", encoding="utf-8"
    )
    (repertoire / "library" / "memory").mkdir()
    (repertoire / "library" / "memory" / "notes.txt").write_text(
        "note\n", encoding="utf-8"
    )

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    (view.root / "library" / "local.md").write_text("# Local\n", encoding="utf-8")

    catalog = asyncio.run(_LibraryCatalog.reconcile(view, _cache(tmp_path)))

    by_path = {entry.path: entry for entry in catalog.entries}
    assert set(by_path) == {
        "resources",
        "resources/design.md",
        "memory",
        "memory/notes.txt",
        "local.md",
    }

    design = by_path["resources/design.md"]
    assert design.kind == "file"
    assert design.provenance == "repertoire"
    assert design.shadows_repertoire is False
    assert design.status == "pending"
    assert design.summary is None
    assert design.error is None
    assert design.fingerprint is not None

    local = by_path["local.md"]
    assert local.provenance == "workspace"
    assert local.shadows_repertoire is False

    resources = by_path["resources"]
    assert resources.kind == "directory"
    assert resources.provenance == "repertoire"
    assert resources.shadows_repertoire is False
    assert resources.fingerprint is None
    assert resources.status == "pending"

    assert catalog.get("resources/design.md") is design
    assert catalog.get("missing") is None


def test_workspace_override_shadows_repertoire_file(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "guide.md").write_text("lower\n", encoding="utf-8")

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    view_md = view.root / "library" / "guide.md"
    assert view_md.is_symlink()
    view._copy_up(view_md)
    view_md.write_text("upper\n", encoding="utf-8")

    catalog = asyncio.run(_LibraryCatalog.reconcile(view, _cache(tmp_path)))

    entry = catalog.get("guide.md")
    assert entry is not None
    assert entry.provenance == "workspace"
    assert entry.shadows_repertoire is True
    assert (repertoire / "library" / "guide.md").read_text() == "lower\n"


def test_workspace_only_directory_is_workspace_provenance(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    (view.root / "library" / "scratch").mkdir()
    (view.root / "library" / "scratch" / "tmp.md").write_text("tmp\n", encoding="utf-8")

    catalog = asyncio.run(_LibraryCatalog.reconcile(view, _cache(tmp_path)))

    scratch = catalog.get("scratch")
    assert scratch is not None
    assert scratch.kind == "directory"
    assert scratch.provenance == "workspace"
    assert scratch.shadows_repertoire is False
    assert catalog.get("scratch/tmp.md").provenance == "workspace"  # type: ignore[union-attr]


def test_whiteouted_library_file_is_skipped(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "gone.md").write_text("gone\n", encoding="utf-8")
    (repertoire / "library" / "kept.md").write_text("kept\n", encoding="utf-8")

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    whiteout = view.root / ".capability-view" / "whiteouts" / "library" / "gone.md"
    whiteout.parent.mkdir(parents=True, exist_ok=True)
    whiteout.touch()
    (view.root / "library" / "gone.md").unlink()

    catalog = asyncio.run(_LibraryCatalog.reconcile(view, _cache(tmp_path)))

    assert catalog.get("gone.md") is None
    assert catalog.get("kept.md") is not None


def test_generated_index_md_is_excluded_at_every_level(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "index.md").write_text("root\n", encoding="utf-8")
    (repertoire / "library" / "resources").mkdir()
    (repertoire / "library" / "resources" / "index.md").write_text(
        "nested\n", encoding="utf-8"
    )
    (repertoire / "library" / "resources" / "design.md").write_text(
        "d\n", encoding="utf-8"
    )

    catalog = _reconcile(tmp_path, repertoire)

    paths = {entry.path for entry in catalog.entries}
    assert "index.md" not in paths
    assert "resources/index.md" not in paths
    assert "resources/design.md" in paths


def test_unsupported_extension_only_affects_its_entry(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "data.bin").write_bytes(b"\x00\x01")

    catalog = _reconcile(tmp_path, repertoire)

    entry = catalog.get("data.bin")
    assert entry is not None
    assert entry.status == "unsupported"
    assert "no parser supports file type" in (entry.error or "")
    assert entry.fingerprint is not None


def test_invalid_utf8_marks_entry_failed(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "bad.md").write_bytes(b"\xff\xfe\x00")

    catalog = _reconcile(tmp_path, repertoire)

    entry = catalog.get("bad.md")
    assert entry is not None
    assert entry.status == "failed"
    assert entry.error == "file is not valid UTF-8"


def test_read_failure_marks_entry_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "locked.md").write_text("locked\n", encoding="utf-8")

    def broken_read_bytes(path: Path) -> bytes:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_bytes", broken_read_bytes)

    catalog = _reconcile(tmp_path, repertoire)

    entry = catalog.get("locked.md")
    assert entry is not None
    assert entry.status == "failed"
    assert "cannot read file" in (entry.error or "")
    assert entry.fingerprint is None


def test_text_parser_supports_only_md_and_txt(tmp_path: Path) -> None:
    parser = TextLibraryFileParser()

    assert parser.supports(tmp_path / "a.md")
    assert parser.supports(tmp_path / "b.txt")
    assert parser.supports(tmp_path / "c.MD") is False
    assert parser.supports(tmp_path / "d.markdown") is False
    assert parser.supports(tmp_path / "e.pdf") is False
    assert parser.supports(tmp_path / "noext") is False


def test_text_parser_returns_complete_normalized_text(tmp_path: Path) -> None:
    parser = TextLibraryFileParser()
    path = tmp_path / "doc.md"
    path.write_bytes(b"# Title\r\n\r\nline two\r\nline three")

    text = asyncio.run(parser.parse(path))

    assert text == "# Title\n\nline two\nline three"


def test_text_parser_rejects_invalid_utf8(tmp_path: Path) -> None:
    parser = TextLibraryFileParser()
    path = tmp_path / "bad.txt"
    path.write_bytes(b"\xff\xfe")

    with pytest.raises(LibraryParseError, match="not valid UTF-8"):
        asyncio.run(parser.parse(path))


def test_file_fingerprint_ignores_name_provenance_and_extension(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "library" / "a.md").write_text("same\n", encoding="utf-8")
    (repertoire / "library" / "b.txt").write_text("same\n", encoding="utf-8")

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    (view.root / "library" / "c.md").write_text("same\n", encoding="utf-8")

    catalog = asyncio.run(_LibraryCatalog.reconcile(view, _cache(tmp_path)))

    fingerprints = {
        entry.fingerprint for entry in catalog.entries if entry.kind == "file"
    }
    assert len(fingerprints) == 1


def test_file_fingerprint_is_content_digest_based() -> None:
    digest = _content_digest(b"hello")

    assert _file_fingerprint(digest) == _file_fingerprint(_content_digest(b"hello"))
    assert _file_fingerprint(digest) != _file_fingerprint(_content_digest(b"hello!"))


def test_file_and_directory_fingerprints_use_separate_domains() -> None:
    digest = _content_digest(b"x")

    assert _file_fingerprint(digest) != _directory_fingerprint((("x", "file", None),))


def test_directory_fingerprint_depends_on_sorted_children_and_summaries() -> None:
    base = (("a.md", "file", "summary-a"), ("b", "directory", None))

    assert _directory_fingerprint(base) == _directory_fingerprint(
        (("a.md", "file", "summary-a"), ("b", "directory", "unavailable"))
    )
    assert _directory_fingerprint(base) != _directory_fingerprint(
        (("a.md", "file", "summary-b"), ("b", "directory", None))
    )
    assert _directory_fingerprint(base) != _directory_fingerprint(
        (("b", "directory", None), ("a.md", "file", "summary-a"))
    )


def test_empty_library_has_no_entries(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)

    catalog = _reconcile(tmp_path, repertoire)

    assert catalog.entries == ()
    assert catalog.get("anything") is None
