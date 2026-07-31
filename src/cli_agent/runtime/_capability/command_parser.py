"""Shell command syntax parsing into CommandParseResult facts."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from cli_agent.runtime._capability.tools.facts import ToolCommand

_DIRECT_MUTATORS = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "dd",
        "install",
        "ln",
        "mkdir",
        "mv",
        "patch",
        "rm",
        "rmdir",
        "tee",
        "touch",
        "truncate",
        "unlink",
    }
)


def _sed_is_in_place(tokens: tuple[str, ...]) -> bool:
    """Return whether the operand tokens request an in-place sed edit."""

    return any(token.startswith("-i") for token in tokens)


@dataclass(frozen=True, slots=True)
class CommandParseResult:
    """Syntax facts established without classifying or executing a command."""

    raw_command: str
    tokens: tuple[str, ...]
    executable_basename: str | None
    tokenization_succeeded: bool
    contains_shell_composition: bool
    contains_output_redirection: bool
    tool: ToolCommand | None = None


def parse_shell_command(raw_command: str) -> CommandParseResult:
    """Parse one command without executing or rewriting it.

    Args:
        raw_command (`str`):
            The exact command string to inspect.

    Returns:
        Syntax facts for the command. Tokenization errors surface through
        ``tokenization_succeeded`` so callers need no exception handling.
    """

    try:
        tokens = shlex.split(raw_command, posix=True, comments=False)
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
        contains_shell_composition=_contains_shell_composition(raw_command),
        contains_output_redirection=bool(
            _shell_output_redirection_targets(raw_command)
        ),
    )


def _contains_shell_composition(raw_command: str) -> bool:
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


def _shell_output_redirection_targets(raw_command: str) -> tuple[str, ...]:
    """Return syntactically explicit file targets for output redirections."""

    try:
        lexer = shlex.shlex(
            raw_command,
            posix=True,
            punctuation_chars="|&;<>",
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = tuple(lexer)
    except ValueError:
        return ()

    targets: list[str] = []
    for index, token in enumerate(tokens[:-1]):
        if token not in {">", ">>", "<>", ">|"}:
            continue
        target = tokens[index + 1]
        if target == "&" or target.startswith("&"):
            continue
        targets.append(target)
    return tuple(targets)
