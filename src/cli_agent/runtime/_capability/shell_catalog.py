"""Runtime-owned facts for builtin shell commands.

The catalog is a pure, immutable classifier over parser ``SimpleCommand``
nodes. It never parses raw command text again, never inspects files and
never returns a policy decision. Every classified invocation keeps a stable
``rule_id`` for diagnostics.

The catalog is a Runtime builtin: models, Workspace files and ordinary
configuration cannot add or override its rules.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from cli_agent.runtime._capability.command_parser import (
    ShellWord,
    SimpleCommand,
    _strip_quotes,
)

_GIT_READ_ONLY_SUBCOMMANDS = frozenset(
    {
        "status",
        "log",
        "diff",
        "show",
        "ls-files",
        "ls-tree",
        "cat-file",
        "rev-parse",
        "rev-list",
        "describe",
        "shortlog",
        "blame",
        "grep",
        "reflog",
        "help",
        "version",
    }
)
_FIND_MUTATING_PREDICATES = frozenset(
    {
        "-delete",
        "-exec",
        "-execdir",
        "-fls",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-ok",
        "-okdir",
    }
)
_INTERPRETERS = frozenset(
    {
        "python",
        "python3",
        "pypy",
        "perl",
        "node",
        "ruby",
        "php",
        "deno",
        "lua",
        "julia",
        "Rscript",
    }
)
_DYNAMIC_EXECUTORS = frozenset(
    {
        "sh",
        "bash",
        "zsh",
        "ksh",
        "fish",
        "dash",
        "eval",
        "sudo",
        "doas",
        "xargs",
        "env",
        "exec",
        "builtin",
        "command",
        "watch",
        "timeout",
        "nohup",
        "script",
    }
)


class ShellEffect(str, Enum):
    """The known effect class of one atomic command invocation."""

    OBSERVE = "observe"
    MUTATE = "mutate"
    CONTROL = "control"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AtomicShellFacts:
    """Immutable atomic facts for one AST SimpleCommand."""

    executable: str | None
    effect: ShellEffect
    parallel_safe: bool
    rule_id: str | None


@dataclass(frozen=True, slots=True)
class ShellCommandSpec:
    """A static rule describing one executable in the builtin catalog."""

    executable: str
    guidance: str | None
    inspect: Callable[[SimpleCommand], AtomicShellFacts]


class _ShellCatalog:
    """Immutable builtin lookup of atomic shell command facts."""

    __slots__ = ("specs", "_by_executable")

    def __init__(self, specs: tuple[ShellCommandSpec, ...]) -> None:
        by_executable: dict[str, ShellCommandSpec] = {}
        for spec in specs:
            if not spec.executable or Path(spec.executable).name != spec.executable:
                raise ValueError("builtin shell specs must use executable basenames")
            if spec.executable in by_executable:
                raise ValueError(f"duplicate builtin shell spec: {spec.executable}")
            by_executable[spec.executable] = spec
        object.__setattr__(self, "specs", specs)
        object.__setattr__(self, "_by_executable", by_executable)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"builtin shell catalog is immutable: {name}")

    def inspect(self, command: SimpleCommand) -> AtomicShellFacts:
        """Return immutable atomic facts for one parsed SimpleCommand.

        Args:
            command (`SimpleCommand`):
                One atomic command from the Shell AST. The catalog never
                re-parses the original command or composition syntax.

        Returns:
            Atomic facts classified by the builtin rules; every executable
            without a matching spec fails closed to UNKNOWN facts.
        """

        executable = _basename(command)
        if executable is None:
            return _unknown_facts(None, "shell.unknown.no-executable")
        spec = self._by_executable.get(executable)
        if spec is None:
            return _unknown_facts(executable, "shell.unknown.executable")
        return spec.inspect(command)

    def guidance(self, executable: str) -> str | None:
        """Return the model-facing guidance for one executable, if any."""

        spec = self._by_executable.get(executable)
        return spec.guidance if spec is not None else None


def _read_spec(executable: str) -> Callable[[SimpleCommand], AtomicShellFacts]:
    """Return an unconditional read observation rule for one executable."""

    def inspect(command: SimpleCommand) -> AtomicShellFacts:
        return AtomicShellFacts(
            executable=_basename(command),
            effect=ShellEffect.OBSERVE,
            parallel_safe=True,
            rule_id=f"shell.observe.{executable}",
        )

    return inspect


def _unknown_spec(executable: str, rule_id: str) -> ShellCommandSpec:
    """Return a fail-closed rule that never grants observation trust."""

    def inspect(command: SimpleCommand) -> AtomicShellFacts:
        return _unknown_facts(_basename(command), rule_id)

    return ShellCommandSpec(executable, None, inspect)


def _rg_inspect(command: SimpleCommand) -> AtomicShellFacts:
    executable = _basename(command)
    if any(
        _arg(token) == "--pre" or _arg(token).startswith("--pre=")
        for token in command.argv
    ):
        return _unknown_facts(executable, "shell.unknown.rg-dynamic")
    return AtomicShellFacts(
        executable=executable,
        effect=ShellEffect.OBSERVE,
        parallel_safe=True,
        rule_id="shell.observe.rg",
    )


def _tail_inspect(command: SimpleCommand) -> AtomicShellFacts:
    executable = _basename(command)
    following = any(
        _arg(token) in {"-f", "-F", "--follow"} or _arg(token).startswith("-f")
        for token in command.argv
    )
    return AtomicShellFacts(
        executable=executable,
        effect=ShellEffect.OBSERVE,
        parallel_safe=not following,
        rule_id="shell.observe.tail-follow" if following else "shell.observe.tail",
    )


def _find_inspect(command: SimpleCommand) -> AtomicShellFacts:
    executable = _basename(command)
    if any(_arg(token) in _FIND_MUTATING_PREDICATES for token in command.argv):
        return _unknown_facts(executable, "shell.unknown.find-dynamic")
    return AtomicShellFacts(
        executable=executable,
        effect=ShellEffect.OBSERVE,
        parallel_safe=True,
        rule_id="shell.observe.find",
    )


def _sed_inspect(command: SimpleCommand) -> AtomicShellFacts:
    executable = _basename(command)
    arguments = [_arg(token) for token in command.argv]
    in_place = any(
        (token.startswith("-") and not token.startswith("--") and "i" in token[1:])
        or token == "--in-place"
        for token in arguments
    )
    if in_place:
        return AtomicShellFacts(
            executable=executable,
            effect=ShellEffect.MUTATE,
            parallel_safe=False,
            rule_id="shell.mutate.sed-in-place",
        )
    if any(token.startswith("-n") for token in arguments):
        return AtomicShellFacts(
            executable=executable,
            effect=ShellEffect.OBSERVE,
            parallel_safe=True,
            rule_id="shell.observe.sed-print",
        )
    return _unknown_facts(executable, "shell.unknown.sed")


def _git_inspect(command: SimpleCommand) -> AtomicShellFacts:
    executable = _basename(command)
    if not command.argv:
        return _unknown_facts(executable, "shell.unknown.git")
    subcommand = _arg(command.argv[0])
    if subcommand in _GIT_READ_ONLY_SUBCOMMANDS:
        return AtomicShellFacts(
            executable=executable,
            effect=ShellEffect.OBSERVE,
            parallel_safe=True,
            rule_id="shell.observe.git-readonly",
        )
    return _unknown_facts(executable, "shell.unknown.git")


def _unknown_facts(executable: str | None, rule_id: str) -> AtomicShellFacts:
    """Return fail-closed facts that never grant read or parallel trust."""

    return AtomicShellFacts(
        executable=executable,
        effect=ShellEffect.UNKNOWN,
        parallel_safe=False,
        rule_id=rule_id,
    )


def _basename(command: SimpleCommand) -> str | None:
    """Return the quote-stripped basename of the executable, if any."""

    if command.executable is None:
        return None
    return Path(_strip_quotes(command.executable.text)).name


def _arg(token: ShellWord) -> str:
    """Return one argv word with its wrapping quotes stripped."""

    return _strip_quotes(token.text)


_BUILTIN_SPECS = (
    # search and discovery
    ShellCommandSpec("rg", "search file contents: rg PATTERN [PATH]", _rg_inspect),
    ShellCommandSpec(
        "grep", "search file contents: grep PATTERN [FILE]", _read_spec("grep")
    ),
    ShellCommandSpec("find", "locate files: find PATH -name PATTERN", _find_inspect),
    # local reads
    ShellCommandSpec("cat", "read a small file in full: cat FILE", _read_spec("cat")),
    ShellCommandSpec(
        "head", "read the start of a file: head -n LINES FILE", _read_spec("head")
    ),
    ShellCommandSpec(
        "tail", "read the end of a file: tail -n LINES FILE", _tail_inspect
    ),
    ShellCommandSpec("nl", "read a file with line numbers: nl FILE", _read_spec("nl")),
    ShellCommandSpec("wc", "count lines, words or bytes: wc FILE", _read_spec("wc")),
    # file and workspace metadata
    ShellCommandSpec("ls", "list directory entries: ls PATH", _read_spec("ls")),
    ShellCommandSpec("stat", "read file metadata: stat FILE", _read_spec("stat")),
    ShellCommandSpec("du", "measure disk usage: du -sh PATH", _read_spec("du")),
    # version control reads
    ShellCommandSpec(
        "git", "read-only git queries: git status/log/diff/show", _git_inspect
    ),
    # parameter-sensitive reads
    ShellCommandSpec(
        "sed", "read line ranges with sed -n: sed -n 'N,MP' FILE", _sed_inspect
    ),
    # interpreters and dynamic executors fail closed
    *(
        _unknown_spec(executable, "shell.unknown.interpreter")
        for executable in sorted(_INTERPRETERS)
    ),
    *(
        _unknown_spec(executable, "shell.unknown.dynamic-execution")
        for executable in sorted(_DYNAMIC_EXECUTORS)
    ),
)

_BUILTIN_SHELL_CATALOG = _ShellCatalog(_BUILTIN_SPECS)
