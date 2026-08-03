"""Host-owned user interaction contracts.

``UserInteraction`` is the always-present Host capability for answering
Runtime-owned questions. The Runtime only converts Policy ``ASK`` results
into standard questions; it never owns or closes the Host interaction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class UserOption:
    """One predeclared choice a Host may return for a question."""

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class UserQuestion:
    """One Runtime-owned question presented to the user."""

    request_id: str
    session_id: str
    prompt: str
    options: tuple[UserOption, ...] = ()


@dataclass(frozen=True, slots=True)
class UserAnswer:
    """One Host answer; ``None`` means cancelled or unanswerable."""

    value: str | None


class UserInteraction(Protocol):
    """Host-owned Runtime-wide question channel."""

    async def ask(self, request: UserQuestion) -> UserAnswer:
        """Return the Host answer for one question."""
