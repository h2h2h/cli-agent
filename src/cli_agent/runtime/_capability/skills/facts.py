"""Pure-data Skill capability facts shared across Runtime layers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """One Skill candidate and its trusted Runtime-open facts.

    ``path`` and ``skill_md`` are managed relative paths inside the
    Capability View (for example ``skills/review`` and
    ``skills/review/SKILL.md``), never Host or Backend filesystem paths.
    """

    name: str
    path: str
    skill_md: str
    provenance: Literal["repertoire", "workspace"] | None
    shadows_repertoire: bool
    valid: bool
    validation_error: str | None
    description: str | None

    @property
    def summary(self) -> str:
        """Return the normalized first non-empty description line."""

        if not self.description:
            return ""
        for line in self.description.splitlines():
            text = " ".join(line.strip().lstrip("#").strip().split())
            if text:
                return text
        return ""
