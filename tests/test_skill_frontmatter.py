from pathlib import Path

import pytest

from cli_agent.runtime._capability.skills.parser import (
    SkillParseError,
    parse_frontmatter,
    validate_metadata,
    validate_skill,
)


def test_parse_frontmatter_returns_metadata_mapping(tmp_path: Path) -> None:
    content = (
        "---\n"
        "name: report-skill\n"
        "description: Build a report.\n"
        "license: MIT\n"
        "metadata:\n"
        "  owner: team\n"
        "---\n"
        "Body text.\n"
    )
    metadata = parse_frontmatter(content)
    assert metadata == {
        "name": "report-skill",
        "description": "Build a report.",
        "license": "MIT",
        "metadata": {"owner": "team"},
    }


def test_parse_frontmatter_rejects_missing_marker() -> None:
    with pytest.raises(SkillParseError, match="must start with"):
        parse_frontmatter("name: skill\n")


def test_parse_frontmatter_rejects_unclosed_marker() -> None:
    with pytest.raises(SkillParseError, match="not closed"):
        parse_frontmatter("---\nname: skill\n")


def test_parse_frontmatter_rejects_non_mapping() -> None:
    with pytest.raises(SkillParseError, match="mapping"):
        parse_frontmatter("---\n---\n")


def test_parse_frontmatter_rejects_invalid_yaml() -> None:
    with pytest.raises(SkillParseError, match="invalid YAML"):
        parse_frontmatter("---\nname: [1, 2\n---\n")


def test_validate_metadata_accepts_documented_fields() -> None:
    metadata = {
        "name": "report-skill",
        "description": "Build a report.",
        "license": "MIT",
        "allowed-tools": "shell",
        "metadata": {"owner": "team"},
        "compatibility": "cli-agent >= 0.1",
    }
    assert validate_metadata(metadata, directory_name="report-skill") == []


def test_validate_metadata_reports_required_fields_and_extra_fields() -> None:
    errors = validate_metadata({"extra": "value"}, directory_name="skill")
    assert "missing required frontmatter field: name" in errors
    assert "missing required frontmatter field: description" in errors
    assert "unexpected frontmatter fields: extra" in errors


def test_validate_metadata_rejects_invalid_name() -> None:
    cases = (
        ("Skill-Name", "must be lowercase"),
        ("skill--name", "consecutive hyphens"),
        ("-skill", "start or end with a hyphen"),
        ("skill-", "start or end with a hyphen"),
        ("skill_name", "only letters, digits, and hyphens"),
        ("skill name", "only letters, digits, and hyphens"),
    )
    for name, expected in cases:
        errors = validate_metadata(
            {"name": name, "description": "d"},
            directory_name="skill",
        )
        assert any(expected in error for error in errors), (name, errors)


def test_validate_metadata_rejects_nonempty_blank_name() -> None:
    errors = validate_metadata(
        {"name": "  ", "description": "d"},
        directory_name="skill",
    )
    assert "field 'name' must be a non-empty string" in errors


def test_validate_metadata_rejects_name_over_length_limit() -> None:
    errors = validate_metadata(
        {"name": "a" * 65, "description": "d"},
        directory_name="a" * 65,
    )
    assert any("character limit" in error for error in errors)


def test_validate_metadata_requires_name_matches_directory() -> None:
    errors = validate_metadata(
        {"name": "other", "description": "d"},
        directory_name="skill",
    )
    assert any("must match the directory name" in error for error in errors)


def test_validate_metadata_rejects_invalid_description() -> None:
    assert any(
        "field 'description' must be a non-empty string" in error
        for error in validate_metadata(
            {"name": "skill", "description": "   "},
            directory_name="skill",
        )
    )
    assert any(
        "character limit" in error
        for error in validate_metadata(
            {"name": "skill", "description": "d" * 1025},
            directory_name="skill",
        )
    )


def test_validate_metadata_rejects_non_string_optional_fields() -> None:
    errors = validate_metadata(
        {
            "name": "skill",
            "description": "d",
            "license": 3,
            "allowed-tools": True,
            "metadata": ["not", "mapping"],
            "compatibility": 7,
        },
        directory_name="skill",
    )
    assert "field 'license' must be a string" in errors
    assert "field 'allowed-tools' must be a string" in errors
    assert any("'metadata' must be a mapping" in error for error in errors)
    assert "field 'compatibility' must be a string" in errors


def test_validate_metadata_rejects_non_string_metadata_values() -> None:
    errors = validate_metadata(
        {
            "name": "skill",
            "description": "d",
            "metadata": {"owner": 7},
        },
        directory_name="skill",
    )
    assert any("'metadata' must be a mapping" in error for error in errors)


def test_validate_skill_requires_valid_frontmatter() -> None:
    errors = validate_skill("name: skill\n", directory_name="skill")
    assert any("must start with" in error for error in errors)


def test_validate_skill_reports_name_and_description_errors() -> None:
    errors = validate_skill(
        "---\nname: Other-Name\n---\n",
        directory_name="skill",
    )
    assert any("must be lowercase" in error for error in errors)
    assert any("must match the directory name" in error for error in errors)
    assert any(
        "missing required frontmatter field: description" in error for error in errors
    )


def test_validate_skill_accepts_a_valid_skill() -> None:
    errors = validate_skill(
        "---\nname: report-skill\ndescription: Build a report.\n---\n",
        directory_name="report-skill",
    )
    assert errors == []
