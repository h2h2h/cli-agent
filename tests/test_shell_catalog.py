"""Unit tests for the builtin shell command catalog."""

import pytest

from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._capability.shell_catalog import (
    _BUILTIN_SHELL_CATALOG,
    AtomicShellFacts,
    ShellEffect,
)


def _facts(raw: str) -> AtomicShellFacts:
    parsed = parse_shell_ast(raw)
    assert parsed.root is not None
    simple = parsed.root
    if not hasattr(simple, "argv"):
        pytest.fail(f"expected an atomic command, got {type(simple).__name__}")
    return _BUILTIN_SHELL_CATALOG.inspect(simple)


@pytest.mark.parametrize(
    "raw",
    (
        "rg pattern src",
        "rg --files",
        "rg -n 'TODO' .",
        "grep -r pattern src",
        "cat file.txt",
        "head -20 file.txt",
        "tail -n 20 file.txt",
        "nl file.txt",
        "wc -l file.txt",
        "ls -la",
        "stat file.txt",
        "du -sh .",
        "find . -name '*.py'",
        "git status",
        "git diff",
        "git show HEAD",
        "git log --oneline -5",
        "git 'status' --short",
    ),
)
def test_builtin_read_commands_are_observations(raw: str) -> None:
    facts = _facts(raw)

    assert facts.effect is ShellEffect.OBSERVE
    assert facts.parallel_safe is True
    assert facts.rule_id.startswith("shell.observe.")


@pytest.mark.parametrize(
    ("raw", "rule_id"),
    (
        ("sed -n '5,10p' file.txt", "shell.observe.sed-print"),
        ("sed -nE 's/x/y/p' file.txt", "shell.observe.sed-print"),
        ("sed -i 's/x/y/' file.txt", "shell.mutate.sed-in-place"),
        ("sed -i.bak 's/x/y/' file.txt", "shell.mutate.sed-in-place"),
        ("sed --in-place 's/x/y/' file.txt", "shell.mutate.sed-in-place"),
        ("sed -ni 's/x/y/' file.txt", "shell.mutate.sed-in-place"),
        ("sed 's/x/y/' file.txt", "shell.unknown.sed"),
        ("sed -e 's/x/y/' file.txt", "shell.unknown.sed"),
        ("find . -delete", "shell.unknown.find-dynamic"),
        ("find . -exec rm {} \\;", "shell.unknown.find-dynamic"),
        ("find . -name '*.py' -ok rm {} \\;", "shell.unknown.find-dynamic"),
        ("rg --pre 'cat' pattern", "shell.unknown.rg-dynamic"),
        ("rg --pre='cat' pattern", "shell.unknown.rg-dynamic"),
        ("git commit -m 'x'", "shell.unknown.git"),
        ("git push", "shell.unknown.git"),
        ("git", "shell.unknown.git"),
        ("git -C /tmp status", "shell.unknown.git"),
    ),
)
def test_parameter_sensitive_rules_are_stable(raw: str, rule_id: str) -> None:
    facts = _facts(raw)

    assert facts.rule_id == rule_id
    if rule_id.startswith("shell.observe."):
        assert facts.effect is ShellEffect.OBSERVE
        assert facts.parallel_safe is True
    elif rule_id.startswith("shell.mutate."):
        assert facts.effect is ShellEffect.MUTATE
        assert facts.parallel_safe is False
    else:
        assert facts.effect is ShellEffect.UNKNOWN
        assert facts.parallel_safe is False


def test_tail_follow_is_observe_but_not_parallel_safe() -> None:
    for raw in ("tail -f /tmp/log", "tail -F /tmp/log", "tail --follow /tmp/log"):
        facts = _facts(raw)

        assert facts.effect is ShellEffect.OBSERVE
        assert facts.parallel_safe is False
        assert facts.rule_id == "shell.observe.tail-follow"


@pytest.mark.parametrize(
    ("raw", "rule_id"),
    (
        ("python task.py", "shell.unknown.interpreter"),
        ("python3 -c 'print(1)'", "shell.unknown.interpreter"),
        ("perl script.pl", "shell.unknown.interpreter"),
        ("node app.js", "shell.unknown.interpreter"),
        ("sh -c 'ls'", "shell.unknown.dynamic-execution"),
        ("bash script.sh", "shell.unknown.dynamic-execution"),
        ("eval 'ls'", "shell.unknown.dynamic-execution"),
        ("sudo apt update", "shell.unknown.dynamic-execution"),
        ("xargs rm", "shell.unknown.dynamic-execution"),
        ("env VAR=1 ls", "shell.unknown.dynamic-execution"),
    ),
)
def test_interpreters_and_dynamic_executors_fail_closed(raw: str, rule_id: str) -> None:
    facts = _facts(raw)

    assert facts.effect is ShellEffect.UNKNOWN
    assert facts.parallel_safe is False
    assert facts.rule_id == rule_id


def test_unknown_executable_never_gets_read_or_parallel_trust() -> None:
    facts = _facts("mystery-tool --read-only arg")

    assert facts.effect is ShellEffect.UNKNOWN
    assert facts.parallel_safe is False
    assert facts.rule_id == "shell.unknown.executable"
    assert facts.executable == "mystery-tool"


def test_absolute_paths_match_by_basename() -> None:
    facts = _facts("/usr/bin/rg pattern")

    assert facts.effect is ShellEffect.OBSERVE
    assert facts.executable == "rg"


def test_command_without_executable_fails_closed() -> None:
    facts = _facts("FOO=bar")

    assert facts.executable is None
    assert facts.effect is ShellEffect.UNKNOWN
    assert facts.parallel_safe is False
    assert facts.rule_id == "shell.unknown.no-executable"


def test_facts_are_immutable_frozen_values() -> None:
    facts = _facts("cat file.txt")

    assert isinstance(facts, AtomicShellFacts)
    with pytest.raises(AttributeError):
        facts.effect = ShellEffect.UNKNOWN
    with pytest.raises(AttributeError):
        facts.parallel_safe = False


def test_catalog_is_builtin_and_not_configurable() -> None:
    with pytest.raises(AttributeError):
        _BUILTIN_SHELL_CATALOG.specs = ()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        _BUILTIN_SHELL_CATALOG.inspect = lambda command: None  # type: ignore[method-assign]


def test_guidance_is_available_for_builtin_read_commands() -> None:
    assert _BUILTIN_SHELL_CATALOG.guidance("rg") is not None
    assert _BUILTIN_SHELL_CATALOG.guidance("cat") is not None
    assert _BUILTIN_SHELL_CATALOG.guidance("sed") is not None
    assert _BUILTIN_SHELL_CATALOG.guidance("git") is not None
    assert _BUILTIN_SHELL_CATALOG.guidance("python") is None
    assert _BUILTIN_SHELL_CATALOG.guidance("mystery-tool") is None
