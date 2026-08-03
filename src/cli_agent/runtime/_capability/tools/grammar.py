"""Reserved Tool grammar classification and Runtime custom command handling."""

from __future__ import annotations

import ast
import re

from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.facts import ToolCommand

_HEREDOC_PATTERN = re.compile(
    r"\Atools[ \t]+run[ \t]+<<[ \t]*(?:'PY'|\"PY\"|PY)[ \t]*\r?\n"
    r"(?P<code>.*)\r?\nPY[ \t]*\Z",
    re.DOTALL,
)
_RUN_PREFIX = re.compile(r"\Atools[ \t]+run(?:[ \t]+(?P<argument>.*))?\Z")


def parse_tool_command(
    command: ShellParseResult,
    catalog: _ToolCatalog,
) -> ToolCommand | None:
    """Parse reserved Tools grammar into independent capability facts."""

    if not _is_reserved_tool_head(command):
        return None
    return _tool_facts(command, catalog)


def _tool_facts(command: ShellParseResult, catalog: _ToolCatalog) -> ToolCommand:
    if (
        command.tokenization_succeeded
        and command.tokens == ("tools", "list")
        and not command.contains_shell_composition
    ):
        return ToolCommand(operation="list", valid=True)

    if (
        command.tokenization_succeeded
        and len(command.tokens) == 3
        and command.tokens[:2] == ("tools", "info")
        and not command.contains_shell_composition
    ):
        name = command.tokens[2]
        entry = catalog.get(name)
        return ToolCommand(
            operation="inspect",
            valid=entry is not None,
            validation_error=(None if entry is not None else f"Tool not found: {name}"),
            name=name,
            references=(entry,) if entry is not None else (),
        )

    normalized = command.raw_command.strip()
    heredoc = _HEREDOC_PATTERN.fullmatch(normalized)
    if heredoc is not None:
        return _run_facts(heredoc.group("code"), catalog)

    quoted = _extract_quoted_run(normalized)
    if quoted is not None:
        return _run_facts(quoted, catalog)

    return ToolCommand(
        operation="invalid",
        valid=False,
        validation_error=(
            "Usage: tools <list|info|run>; run accepts one quoted Python "
            "payload or exact <<'PY' ... PY heredoc syntax"
        ),
    )


def _run_facts(code: str, catalog: _ToolCatalog) -> ToolCommand:
    try:
        tree = ast.parse(code, filename="<tools run>")
    except SyntaxError as exc:
        return ToolCommand(
            operation="run",
            valid=False,
            validation_error=f"SyntaxError: {exc}",
            code=code,
        )

    names, dynamic = _tool_references(tree)
    entries = tuple(catalog.get(name) for name in names)
    missing = tuple(
        name for name, entry in zip(names, entries, strict=True) if entry is None
    )
    invalid = tuple(entry for entry in entries if entry is not None and not entry.valid)
    error = None
    if missing:
        error = f"Tool not found: {', '.join(missing)}"
    elif invalid:
        error = "; ".join(
            f"{entry.name}: {entry.validation_error or 'invalid Tool'}"
            for entry in invalid
        )

    return ToolCommand(
        operation="run",
        valid=error is None,
        validation_error=error,
        code=code,
        references=tuple(entry for entry in entries if entry is not None),
        has_dynamic_references=dynamic,
    )


def _is_reserved_tool_head(command: ShellParseResult) -> bool:
    if command.tokens and command.tokens[0] == "tools":
        return True
    return bool(re.match(r"\A[ \t]*tools(?:[ \t\r\n]|\Z)", command.raw_command))


def _extract_quoted_run(raw_command: str) -> str | None:
    match = _RUN_PREFIX.fullmatch(raw_command)
    if match is None:
        return None
    argument = match.group("argument")
    if argument is None or "\n" in argument or "\r" in argument:
        return None
    for quote in ('"""', "'''", '"', "'"):
        if (
            argument.startswith(quote)
            and argument.endswith(quote)
            and len(argument) >= len(quote) * 2
        ):
            return argument[len(quote) : -len(quote)]
    return None


def _tool_references(tree: ast.AST) -> tuple[tuple[str, ...], bool]:
    visitor = _ToolsNamespaceVisitor()
    visitor.visit(tree)
    return tuple(sorted(visitor.names)), visitor.dynamic


class _ToolsNamespaceVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.names: set[str] = set()
        self.dynamic = False

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and node.value.id == "tools":
            self.names.add(node.attr)
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "tools":
            self.dynamic = True

    def visit_Call(self, node: ast.Call) -> None:
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "tools"
        ):
            self.dynamic = True
        self.generic_visit(node)
