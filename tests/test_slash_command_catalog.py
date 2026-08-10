"""Tests for the application-level slash command catalog."""

import ast
import importlib
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from cli_agent.slash_commands import CommandAction, CommandSpec, resolve, specs


def test_catalog_contains_only_exit_with_display_metadata() -> None:
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "exit"
    assert spec.description
    assert spec.action is CommandAction.EXIT


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
    ["/EXIT", "/Exit", "/exit now", "/unknown", "foo /exit", "", "exit"],
)
def test_resolve_passes_through_unknown_or_modified_input(text: str) -> None:
    assert resolve(text) is CommandAction.PASS


def test_dispatch_names_derive_from_spec_names() -> None:
    expected = {f"/{spec.name}" for spec in specs}
    for name in expected:
        assert resolve(name) is not CommandAction.PASS
    assert resolve("exit") is CommandAction.PASS
    assert resolve("/") is CommandAction.PASS


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
