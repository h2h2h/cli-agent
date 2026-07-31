"""Pure-data Tool capability facts shared across Runtime layers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class ToolEntry:
    """One Tool candidate and its trusted Runtime-open facts."""

    name: str
    path: Path
    provenance: Literal["repertoire", "workspace"] | None
    shadows_repertoire: bool
    valid: bool
    validation_error: str | None
    documentation: str | None

    @property
    def summary(self) -> str:
        if not self.documentation:
            return ""
        for line in self.documentation.splitlines():
            text = line.strip().lstrip("#").strip()
            if text:
                return text
        return ""


@dataclass(frozen=True, slots=True)
class ToolCommand:
    """Trusted classification of one reserved top-level ``tools`` command."""

    operation: Literal["list", "inspect", "run", "invalid"]
    valid: bool
    validation_error: str | None = None
    name: str | None = None
    code: str | None = None
    references: tuple[ToolEntry, ...] = ()
    has_dynamic_references: bool = False
