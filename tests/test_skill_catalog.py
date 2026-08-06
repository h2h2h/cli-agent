from pathlib import Path

from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.view import _CapabilityView
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._system_message import assemble_system_message


def _skill(directory: Path, name: str, description: str = "d") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )


def _repertoire(workspace: Path) -> Path:
    repertoire = workspace.parent / f"{workspace.name}-repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    return repertoire


def test_catalog_generates_index_and_reports_actual_provenance(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    _skill(repertoire / "skills" / "lower-skill", "lower-skill", "Lower skill.")
    lower_index = repertoire / "skills" / "index.md"
    lower_index.write_text("user-owned lower index\n")

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    _skill(view.root / "skills" / "local-skill", "local-skill", "Local skill.")

    catalog = _SkillCatalog.reconcile(view)

    assert catalog.get("lower-skill").provenance == "repertoire"  # type: ignore[union-attr]
    assert catalog.get("local-skill").provenance == "workspace"  # type: ignore[union-attr]
    assert catalog.get("lower-skill").description == "Lower skill."  # type: ignore[union-attr]
    assert catalog.get("lower-skill").shadows_repertoire is False  # type: ignore[union-attr]

    index = view.root / "skills" / "index.md"
    assert index.is_file()
    assert not index.is_symlink()
    content = index.read_text()
    assert "lower-skill | valid | repertoire" in content
    assert "local-skill | valid | workspace" in content
    assert "Lower skill." in content
    assert lower_index.read_text() == "user-owned lower index\n"

    text, found = catalog.render_info("lower-skill")
    assert found is True
    assert "Provenance: repertoire" in text
    assert "Lower skill." in text


def test_catalog_reports_structural_validation_errors(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    _skill(repertoire / "skills" / "wrong-name", "actual-name")
    _skill(repertoire / "skills" / "bad-yaml", "bad-yaml")
    (repertoire / "skills" / "bad-yaml" / "SKILL.md").write_text(
        "---\nname: [1, 2\n---\n",
        encoding="utf-8",
    )
    (repertoire / "skills" / "empty-skill").mkdir(parents=True)

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)

    catalog = _SkillCatalog.reconcile(view)

    wrong = catalog.get("wrong-name")
    assert wrong is not None
    assert wrong.valid is False
    assert "must match the directory name" in wrong.validation_error
    assert wrong.provenance == "repertoire"

    bad = catalog.get("bad-yaml")
    assert bad is not None
    assert bad.valid is False
    assert "invalid YAML" in bad.validation_error

    empty = catalog.get("empty-skill")
    assert empty is not None
    assert empty.valid is False
    assert empty.validation_error == "missing required file: SKILL.md"

    text, found = catalog.render_info("empty-skill")
    assert found is True
    assert "missing required file: SKILL.md" in text

    index = view.root / "skills" / "index.md"
    assert "wrong-name | invalid: skill name" in index.read_text()


def test_catalog_skips_whiteouted_skills_and_ignores_non_directories(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    _skill(repertoire / "skills" / "gone-skill", "gone-skill")
    _skill(repertoire / "skills" / "kept-skill", "kept-skill")

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    whiteout = (
        view.root / ".capability-view" / "whiteouts" / "skills" / "gone-skill" / "SKILL.md"
    )
    whiteout.parent.mkdir(parents=True, exist_ok=True)
    whiteout.touch()
    (view.root / "skills" / "gone-skill" / "SKILL.md").unlink()
    (view.root / "skills" / "not-a-skill.py").write_text("VALUE = 1\n")

    catalog = _SkillCatalog.reconcile(view)

    assert catalog.get("gone-skill") is None
    assert catalog.get("not-a-skill.py") is None
    assert catalog.get("kept-skill").valid is True  # type: ignore[union-attr]
    index = view.root / "skills" / "index.md"
    assert "gone-skill" not in index.read_text()


def test_catalog_reports_mixed_directory_provenance_from_skill_md(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    _skill(repertoire / "skills" / "mixed-skill", "mixed-skill")

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    (view.root / "skills" / "mixed-skill" / "notes.txt").write_text("workspace note\n")

    catalog = _SkillCatalog.reconcile(view)

    entry = catalog.get("mixed-skill")
    assert entry is not None
    assert entry.provenance == "repertoire"
    assert entry.valid is True


def test_catalog_reports_workspace_override_shadowing_repertoire(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    _skill(repertoire / "skills" / "override-skill", "override-skill", "Lower.")
    lower_md = repertoire / "skills" / "override-skill" / "SKILL.md"

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    view_md = view.root / "skills" / "override-skill" / "SKILL.md"
    assert view_md.is_symlink()
    view._copy_up(view_md)
    view_md.write_text(
        "---\nname: override-skill\ndescription: Override.\n---\n",
        encoding="utf-8",
    )

    catalog = _SkillCatalog.reconcile(view)

    entry = catalog.get("override-skill")
    assert entry is not None
    assert entry.provenance == "workspace"
    assert entry.shadows_repertoire is True
    assert entry.description == "Override."
    assert lower_md.read_text() == (
        "---\nname: override-skill\ndescription: Lower.\n---\n"
    )


def test_catalog_index_is_reproducible_and_never_authority(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    _skill(repertoire / "skills" / "first-skill", "first-skill", "First.")
    _skill(repertoire / "skills" / "second-skill", "second-skill", "Second.")

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    first = _SkillCatalog.reconcile(view).render_index()
    second = _SkillCatalog.reconcile(view).render_index()

    assert first == second
    authored = view.root / "skills" / "index.md"
    authored.write_text("model-authored index\n")
    catalog = _SkillCatalog.reconcile(view)
    assert catalog.get("first-skill") is not None
    assert authored.read_text() != "model-authored index\n"


def test_catalog_get_and_valid_entries(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    _skill(repertoire / "skills" / "ok-skill", "ok-skill")
    _skill(repertoire / "skills" / "bad-name", "other-name")

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    catalog = _SkillCatalog.reconcile(view)

    assert catalog.get("ok-skill").valid is True  # type: ignore[union-attr]
    assert catalog.get("bad-name").valid is False  # type: ignore[union-attr]
    assert catalog.get("missing") is None
    assert {entry.name for entry in catalog.valid_entries} == {"ok-skill"}
    assert len(catalog.entries) == 2


def test_catalog_render_info_reports_missing_skill(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    catalog = _SkillCatalog.reconcile(view)

    text, found = catalog.render_info("missing")
    assert found is False
    assert text == "Skill not found: missing\n"


def test_system_message_embeds_only_compact_skills_catalog(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    _skill(repertoire / "skills" / "banner-skill", "banner-skill", "Banner helper.")
    _skill(repertoire / "skills" / "broken-skill", "wrong-name")

    _prepare_workspace(tmp_path)
    view = _CapabilityView.open(tmp_path, repertoire)
    catalog = _SkillCatalog.reconcile(view)

    message = assemble_system_message(tmp_path, None, skill_catalog=catalog)
    body = "\n".join(block.text for block in message.content)

    assert "Skills" in body
    assert "| banner-skill | valid | Banner helper. |" in body
    assert "| broken-skill | invalid" in body
    assert "cat .workspace/skills/<name>/SKILL.md" in body
    assert "name: banner-skill" not in body
    assert "description: Banner helper." not in body


def test_system_message_skill_section_omitted_without_catalog(
    tmp_path: Path,
) -> None:
    message = assemble_system_message(tmp_path, None)
    body = "\n".join(block.text for block in message.content)

    assert "\n\n**Skills**\n" not in body
    assert "No Skills are currently discovered." not in body
