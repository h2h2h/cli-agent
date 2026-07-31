"""Strict SKILL.md frontmatter parsing and structural validation."""

from __future__ import annotations

import os
import unicodedata
from pathlib import Path
from typing import Mapping

import strictyaml

_SKILL_MD_FILENAME = "SKILL.md"

_MAX_SKILL_NAME_LENGTH = 64
_MAX_DESCRIPTION_LENGTH = 1024

_ALLOWED_FIELDS = frozenset(
    {
        "name",
        "description",
        "license",
        "allowed-tools",
        "metadata",
        "compatibility",
    }
)


class SkillParseError(ValueError):
    """Raised when the SKILL.md frontmatter is missing or malformed."""


def find_skill_md(skill_directory: Path) -> Path | None:
    """Return the exact ``SKILL.md`` path inside one Skill directory.

    The name match is case-sensitive even on case-insensitive filesystems, so
    a lowercase ``skill.md`` is not treated as the required definition file.
    """

    try:
        names = os.listdir(skill_directory)
    except OSError:
        return None
    if _SKILL_MD_FILENAME not in names:
        return None
    candidate = skill_directory / _SKILL_MD_FILENAME
    return candidate if candidate.is_file() else None


def parse_frontmatter(content: str) -> dict[str, object]:
    """Parse one strict YAML frontmatter mapping without trusting its values.

    Args:
        content (`str`):
            The full SKILL.md text.

    Returns:
        The parsed frontmatter mapping.

    Raises:
        SkillParseError: If the frontmatter is missing, unclosed, or not a
            YAML mapping.
    """

    if not content.startswith("---"):
        raise SkillParseError("SKILL.md must start with YAML frontmatter (---)")

    parts = content.split("---", 2)
    if len(parts) < 3:
        raise SkillParseError("SKILL.md frontmatter is not closed with ---")

    frontmatter = parts[1]
    try:
        parsed = strictyaml.load(frontmatter)
    except strictyaml.YAMLError as exc:
        raise SkillParseError(f"invalid YAML in frontmatter: {exc}") from exc

    metadata = parsed.data
    if not isinstance(metadata, dict):
        raise SkillParseError("SKILL.md frontmatter must be a YAML mapping")
    return metadata


def validate_metadata(
    metadata: Mapping[str, object],
    *,
    directory_name: str,
) -> list[str]:
    """Validate parsed frontmatter against the documented Skill schema.

    Returns aggregated structural errors without raising so the caller can
    record them on a catalog entry.
    """

    errors: list[str] = []

    extra_fields = set(metadata) - _ALLOWED_FIELDS
    if extra_fields:
        errors.append(
            "unexpected frontmatter fields: "
            + ", ".join(sorted(extra_fields))
        )

    if "name" not in metadata:
        errors.append("missing required frontmatter field: name")
    else:
        errors.extend(_validate_name(metadata["name"], directory_name))

    if "description" not in metadata:
        errors.append("missing required frontmatter field: description")
    else:
        errors.extend(_validate_description(metadata["description"]))

    for field in ("license", "allowed-tools", "compatibility"):
        if field in metadata and not isinstance(metadata[field], str):
            errors.append(f"field '{field}' must be a string")

    if "metadata" in metadata:
        nested = metadata["metadata"]
        if not isinstance(nested, Mapping):
            errors.append("field 'metadata' must be a mapping of strings to strings")
        else:
            for key, value in nested.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    errors.append(
                        "field 'metadata' must be a mapping of strings to strings"
                    )
                    break

    return errors


def validate(skill_directory: Path) -> list[str]:
    """Return aggregated structural errors for one Skill directory."""

    if not skill_directory.is_dir():
        return [f"not a directory: {skill_directory}"]

    skill_md = find_skill_md(skill_directory)
    if skill_md is None:
        return ["missing required file: SKILL.md"]

    try:
        content = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [f"SKILL.md is not readable UTF-8: {exc}"]

    try:
        metadata = parse_frontmatter(content)
    except SkillParseError as exc:
        return [str(exc)]

    return validate_metadata(metadata, directory_name=skill_directory.name)


def _validate_name(name: object, directory_name: str) -> list[str]:
    if not isinstance(name, str) or not name.strip():
        return ["field 'name' must be a non-empty string"]

    normalized = unicodedata.normalize("NFKC", name.strip())
    errors: list[str] = []
    if len(normalized) > _MAX_SKILL_NAME_LENGTH:
        errors.append(
            f"skill name exceeds {_MAX_SKILL_NAME_LENGTH} character limit"
        )
    if normalized != normalized.lower():
        errors.append("skill name must be lowercase")
    if normalized.startswith("-") or normalized.endswith("-"):
        errors.append("skill name cannot start or end with a hyphen")
    if "--" in normalized:
        errors.append("skill name cannot contain consecutive hyphens")
    if not all(
        character.isalnum() or character == "-" for character in normalized
    ):
        errors.append("skill name may contain only letters, digits, and hyphens")

    directory = unicodedata.normalize("NFKC", directory_name)
    if normalized != directory:
        errors.append(
            f"skill name {normalized!r} must match the directory name "
            f"{directory!r}"
        )
    return errors 


def _validate_description(description: object) -> list[str]:
    if not isinstance(description, str) or not description.strip():
        return ["field 'description' must be a non-empty string"]
    if len(description) > _MAX_DESCRIPTION_LENGTH:
        return [
            f"description exceeds {_MAX_DESCRIPTION_LENGTH} character limit"
        ]
    return []
