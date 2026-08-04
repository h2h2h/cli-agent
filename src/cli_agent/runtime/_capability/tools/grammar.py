"""Reserved Tool grammar classification and Runtime custom command handling."""

from __future__ import annotations

import ast

from cli_agent.runtime._capability.command_parser import (
    HereDocRedirect,
    ShellParseResult,
    ShellWord,
    SimpleCommand,
)
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.facts import ToolCommand


def parse_tool_command(
    command: ShellParseResult,
    catalog: _ToolCatalog,
) -> ToolCommand | None:
    """Parse reserved Tools grammar into independent capability facts."""

    if command.command_head != "tools":
        return None
    root = command.root
    if not isinstance(root, SimpleCommand) or root.prefix_assignments:
        return _invalid_tool_command()
    return _tool_facts(root, catalog)


def _tool_facts(command: SimpleCommand, catalog: _ToolCatalog) -> ToolCommand:
    match command.argv, command.redirects:
        case (ShellWord(value="list"),), ():
            return ToolCommand(operation="list", valid=True)

        case (ShellWord(value="info"), ShellWord(value=name)), () if name is not None:
            return _info_facts(name, catalog)

        case (
            (ShellWord(value="run"), ShellWord(quote=quote) as payload),
            (),
        ) if (
            quote is not None and "\n" not in payload.text and "\r" not in payload.text
        ):
            code = payload.quoted_content
            if code is not None:
                return _run_facts(code, catalog)

        case (
            (ShellWord(value="run"),),
            (HereDocRedirect() as heredoc,),
        ) if heredoc.operator == "<<" and heredoc.delimiter.text in {
            "PY",
            "'PY'",
            '"PY"',
        }:
            return _run_facts(_strip_terminal_line_break(heredoc.body.text), catalog)

    return _invalid_tool_command()


def _info_facts(name: str, catalog: _ToolCatalog) -> ToolCommand:
    """Resolve one statically named Tool catalog entry."""

    entry = catalog.get(name)
    return ToolCommand(
        operation="inspect",
        valid=entry is not None,
        validation_error=(None if entry is not None else f"Tool not found: {name}"),
        name=name,
        references=(entry,) if entry is not None else (),
    )


def _invalid_tool_command() -> ToolCommand:
    """Return the stable diagnostic for an unsupported Tools command shape."""

    return ToolCommand(
        operation="invalid",
        valid=False,
        validation_error=(
            "Usage: tools <list|info|run>; run accepts one quoted Python "
            "payload or exact <<'PY' ... PY heredoc syntax"
        ),
    )


def _strip_terminal_line_break(text: str) -> str:
    """Remove the line break separating a heredoc body from its delimiter."""

    if text.endswith("\r\n"):
        return text[:-2]
    return text.removesuffix("\n")


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
