"""Structured host-directed notices emitted by the headless Runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping


@dataclass(frozen=True, slots=True)
class RuntimeDiagnostic:
    """One structured notice emitted by the headless Runtime for its host.

    Diagnostics inform the host about non-blocking Runtime-open reconcile
    events without changing Runtime-open success. They never carry env
    values, credentials, or Secret References.
    """

    kind: str
    message: str
    detail: Mapping[str, object] = field(default_factory=dict)
