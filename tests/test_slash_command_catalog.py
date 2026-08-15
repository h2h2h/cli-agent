"""Tests for the application-level slash command catalog."""

import ast
import importlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cli_agent.slash_commands import (
    CommandAction,
    CommandSpec,
    parse,
    resolve,
    specs,
)


def test_catalog_contains_exit_and_usage_with_display_metadata() -> None:
    assert len(specs) == 5
    exit_spec = specs[0]
    assert exit_spec.name == "exit"
    assert exit_spec.description
    assert exit_spec.action is CommandAction.EXIT
    usage_spec = specs[1]
    assert usage_spec.name == "usage"
    assert usage_spec.description
    assert usage_spec.action is CommandAction.USAGE
    assert [spec.name for spec in specs[2:]] == ["new", "sessions", "resume"]


def test_specs_is_read_only_stable_sequence() -> None:
    assert type(specs) is tuple


def test_command_spec_is_immutable() -> None:
    spec = CommandSpec(
        name="exit",
        description="End the current interactive session",
        action=CommandAction.EXIT,
    )
    with pytest.raises(FrozenInstanceError):
        spec.name = "quit"


@pytest.mark.parametrize(
    "text",
    ["/exit", " /exit", "/exit ", "  /exit  ", "\t/exit\n"],
)
def test_resolve_matches_exit_with_surrounding_whitespace(text: str) -> None:
    assert resolve(text) is CommandAction.EXIT


@pytest.mark.parametrize(
    "text",
    ["/usage", " /usage", "/usage ", "  /usage  ", "\t/usage\n"],
)
def test_resolve_matches_usage_with_surrounding_whitespace(text: str) -> None:
    assert resolve(text) is CommandAction.USAGE


@pytest.mark.parametrize(
    "text",
    ["/EXIT", "/Exit", "/exit now", "/unknown", "foo /exit", "", "exit"],
)
def test_resolve_passes_through_unknown_or_modified_input(text: str) -> None:
    assert resolve(text) is CommandAction.PASS


def test_dispatch_names_derive_from_spec_names() -> None:
    for spec in specs:
        name = f"/{spec.name}"
        if spec.argument_count:
            name += " session-1"
        assert resolve(name) is spec.action
    assert resolve("exit") is CommandAction.PASS
    assert resolve("/") is CommandAction.PASS


def test_parse_validates_session_command_arguments() -> None:
    invocation = parse("/resume session-1")
    assert invocation is not None
    assert invocation.action is CommandAction.RESUME
    assert invocation.valid
    assert invocation.arguments == ("session-1",)
    assert invocation.usage == "/resume <session_id>"

    malformed = parse("/resume")
    assert malformed is not None
    assert not malformed.valid
    assert resolve("/resume") is CommandAction.PASS


def test_slash_commands_package_does_not_import_tui_or_runtime_types() -> None:
    package_dir = Path(
        importlib.import_module("cli_agent.slash_commands").__file__
    ).parent
    for source_file in package_dir.glob("*.py"):
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not _imports_forbidden(alias.name), (
                        source_file.name,
                        alias.name,
                    )
            elif isinstance(node, ast.ImportFrom):
                imported = node.module or ""
                assert not _imports_forbidden(imported), (source_file.name, imported)


def _imports_forbidden(name: str) -> bool:
    return name in {
        "prompt_toolkit",
        "cli_agent.runtime",
        "cli_agent.tui",
        "cli_agent.config",
    } or name.startswith(
        ("prompt_toolkit.", "cli_agent.runtime.", "cli_agent.tui.", "cli_agent.config.")
    )
