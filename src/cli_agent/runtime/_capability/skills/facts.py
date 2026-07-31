"""Pure-data Skill capability facts shared across Runtime layers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class SkillEntry:
    """One Skill candidate and its trusted Runtime-open facts."""

    name: str
    path: Path
    skill_md: Path
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
