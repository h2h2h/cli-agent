"""Application-level catalog for built-in slash commands."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from enum import Enum, auto


class CommandAction(Enum):
    """Local action produced by resolving an interactive input."""

    PASS = auto()
    EXIT = auto()
    USAGE = auto()
    NEW = auto()
    SESSIONS = auto()
    RESUME = auto()


@dataclass(frozen=True)
class CommandSpec:
    """Immutable metadata for a single built-in slash command."""

    name: str
    description: str
    action: CommandAction
    argument_count: int = 0


@dataclass(frozen=True)
class CommandInvocation:
    """One parsed slash command and its positional arguments."""

    spec: CommandSpec
    arguments: tuple[str, ...]

    @property
    def action(self) -> CommandAction:
        """Return the catalog action represented by this invocation."""

        return self.spec.action

    @property
    def valid(self) -> bool:
        """Return whether the invocation satisfies the spec grammar."""

        return len(self.arguments) == self.spec.argument_count

    @property
    def usage(self) -> str:
        """Return the stable help syntax for this invocation's command."""

        if self.spec.argument_count == 0:
            return f"/{self.spec.name}"
        return f"/{self.spec.name} <session_id>"


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
    CommandSpec(
        name="new",
        description="Create and activate a new Session",
        action=CommandAction.NEW,
    ),
    CommandSpec(
        name="sessions",
        description="List resumable Sessions",
        action=CommandAction.SESSIONS,
    ),
    CommandSpec(
        name="resume",
        description="Resume a Session in this Workspace",
        action=CommandAction.RESUME,
        argument_count=1,
    ),
)

_DISPATCH: dict[str, CommandAction] = {f"/{spec.name}": spec.action for spec in specs}
_SPEC_BY_COMMAND = {f"/{spec.name}": spec for spec in specs}


def parse(text: str) -> CommandInvocation | None:
    """Parse a slash command using the catalog's argument grammar.

    Unknown input returns ``None`` so it can continue through the normal
    Agent prompt path. Known commands with invalid arity return an invalid
    invocation, allowing the Host to show command help instead of sending a
    malformed lifecycle request to the model.
    """

    try:
        words = tuple(shlex.split(text.strip()))
    except ValueError:
        return None
    if not words:
        return None
    spec = _SPEC_BY_COMMAND.get(words[0])
    if spec is None:
        return None
    return CommandInvocation(spec=spec, arguments=words[1:])


def resolve(text: str) -> CommandAction:
    """Resolve interactive input to a command action.

    Args:
        text (`str`): Input as read from the interactive session.

    Returns:
        The valid `CommandAction` after parsing the command and its
        arguments, or `PASS` when the input is unknown or malformed.
    """
    invocation = parse(text)
    if invocation is None or not invocation.valid:
        return CommandAction.PASS
    return _DISPATCH[f"/{invocation.spec.name}"]
