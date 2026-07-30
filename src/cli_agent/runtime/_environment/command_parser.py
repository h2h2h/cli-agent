"""Command parsing strategy and syntax-only CommandParseResult facts."""

from __future__ import annotations

import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CommandParseResult:
    """Syntax facts established without classifying or executing a command."""

    raw_command: str
    tokens: tuple[str, ...]
    executable_basename: str | None
    tokenization_succeeded: bool
    contains_shell_composition: bool


class CommandParser(ABC):
    """Parse one raw Shell command into a validated CommandParseResult.

    Contract: implementations always return a result. Tokenization errors
    surface through ``tokenization_succeeded`` so callers need no exception
    handling.
    """

    @abstractmethod
    def parse(self, raw_command: str) -> CommandParseResult:
        """Parse one command without executing or rewriting it."""


class ShlexCommandParser(CommandParser):
    """Parse Shell commands using the standard-library shlex lexer."""

    def parse(self, raw_command: str) -> CommandParseResult:
        try:
            tokens = self._split_shell_command(raw_command)
        except ValueError:
            parsed_tokens: tuple[str, ...] = ()
            executable = None
            tokenization_succeeded = False
        else:
            parsed_tokens = tuple(tokens)
            executable = Path(tokens[0]).name if tokens else None
            tokenization_succeeded = True

        return CommandParseResult(
            raw_command=raw_command,
            tokens=parsed_tokens,
            executable_basename=executable,
            tokenization_succeeded=tokenization_succeeded,
            contains_shell_composition=self._contains_shell_composition(raw_command),
        )

    def _split_shell_command(self, raw_command: str) -> list[str]:
        return shlex.split(raw_command, posix=True, comments=False)

    def _contains_shell_composition(self, raw_command: str) -> bool:
        quote: str | None = None
        escaped = False
        index = 0
        while index < len(raw_command):
            character = raw_command[index]
            if escaped:
                escaped = False
            elif character == "\\" and quote != "'":
                escaped = True
            elif quote is None and character in {"'", '"'}:
                quote = character
            elif character == quote:
                quote = None
            elif quote is None and character in {"\r", "\n", ";", "|", "&", "<", ">"}:
                return True
            elif quote != "'" and (
                character == "`"
                or (character == "$" and raw_command[index + 1 : index + 2] == "(")
            ):
                return True
            index += 1
        return False


def parse_shell_command(raw_command: str) -> CommandParseResult:
    """Parse one command with the default :class:`ShlexCommandParser`."""
    return ShlexCommandParser().parse(raw_command)
