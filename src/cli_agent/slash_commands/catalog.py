"""Application-level catalog for built-in slash commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto


class CommandAction(Enum):
    """Local action produced by resolving an interactive input."""

    PASS = auto()
    EXIT = auto()
    USAGE = auto()


@dataclass(frozen=True)
class CommandSpec:
    """Immutable metadata for a single built-in slash command."""

    name: str
    description: str
    action: CommandAction


specs: tuple[CommandSpec, ...] = (
    CommandSpec(
        name="exit",
        description="End the current interactive session",
        action=CommandAction.EXIT,
    ),
    CommandSpec(
        name="usage",
        description="Show the session's total input and output tokens",
        action=CommandAction.USAGE,
    ),
)

_DISPATCH: dict[str, CommandAction] = {f"/{spec.name}": spec.action for spec in specs}


def resolve(text: str) -> CommandAction:
    """Resolve interactive input to a command action.

    Args:
        text (`str`): Input as read from the interactive session.

    Returns:
        The `CommandAction` of the exact slash command after stripping
        surrounding whitespace, or `PASS` when nothing matches.
    """
    return _DISPATCH.get(text.strip(), CommandAction.PASS)
