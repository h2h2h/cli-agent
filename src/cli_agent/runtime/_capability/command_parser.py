"""Shell command syntax parsing into a Shell AST and derived syntax facts.

The parser only establishes syntax structure. It does not classify command
semantics, authorize, or execute anything. ``parse_shell_ast`` is the single
entry point and produces one immutable ``ShellParseResult`` per command.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import tree_sitter_bash
from tree_sitter import Language, Node, Parser


@dataclass(frozen=True, slots=True)
class ShellSpan:
    """A byte offset range into the original command text."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ShellWord:
    """One raw word token with its source span."""

    text: str
    span: ShellSpan


@dataclass(frozen=True, slots=True)
class ShellRedirect:
    """One input or output redirection of a shell statement."""

    operator: str
    target: ShellWord | None
    is_output: bool
    span: ShellSpan


@dataclass(frozen=True, slots=True)
class SimpleCommand:
    """An atomic command: an executable, its arguments and attached redirects."""

    executable: ShellWord | None
    argv: tuple[ShellWord, ...]
    redirects: tuple[ShellRedirect, ...]
    has_command_substitution: bool
    span: ShellSpan


@dataclass(frozen=True, slots=True)
class Pipeline:
    """Statements joined by ``|`` or ``|&``, executed as a data flow."""

    elements: tuple[ShellNode, ...]
    span: ShellSpan


@dataclass(frozen=True, slots=True)
class SequenceElement:
    """One statement in a sequence with its trailing separator."""

    command: ShellNode
    terminator: str | None


@dataclass(frozen=True, slots=True)
class Sequence:
    """Statements joined by ``&&``, ``||``, ``;`` or a background ``&``."""

    elements: tuple[SequenceElement, ...]
    span: ShellSpan


@dataclass(frozen=True, slots=True)
class Subshell:
    """A parenthesized command list executed in a child shell."""

    body: ShellNode | None
    span: ShellSpan


@dataclass(frozen=True, slots=True)
class RedirectedCommand:
    """A non-atomic statement whose redirects bind to the whole statement."""

    command: ShellNode
    redirects: tuple[ShellRedirect, ...]
    span: ShellSpan


@dataclass(frozen=True, slots=True)
class UnsupportedCommand:
    """A statement kind the parser does not break down (if/for/function...)."""

    node_type: str
    span: ShellSpan


ShellNode = (
    SimpleCommand
    | Pipeline
    | Sequence
    | Subshell
    | RedirectedCommand
    | UnsupportedCommand
)

_STATEMENT_NODES = frozenset(
    {
        "command",
        "redirected_statement",
        "pipeline",
        "list",
        "subshell",
        "declaration_command",
        "unset_command",
        "negated_command",
        "compound_statement",
        "variable_assignment",
        "for_statement",
        "if_statement",
        "while_statement",
        "case_statement",
        "c_style_for_statement",
        "function_definition",
        "do_group",
    }
)
_SEPARATOR_NODES = frozenset({"&&", "||", ";", "&"})
_REDIRECT_NODES = frozenset(
    {"file_redirect", "heredoc_redirect", "herestring_redirect"}
)
_REDIRECT_OPERATOR_NODES = frozenset(
    {">", ">>", "<", "<>", ">|", ">&", "&>", "<<", "<<-", "<<<"}
)
_DECLARATION_KEYWORDS = frozenset(
    {"export", "unset", "declare", "readonly", "local", "typeset"}
)
_WORD_NODES = frozenset(
    {
        "word",
        "number",
        "raw_string",
        "string",
        "heredoc_start",
        "concatenation",
        "expansion",
        "simple_expansion",
        "variable_assignment",
        "environment_assignment",
        "command_substitution",
        "ansi_c_string",
    }
)
_REDIRECTION_TOKEN_PATTERN = re.compile(r"\d*(?:>>?|<>|>\|)")

_PARSER = Parser(Language(tree_sitter_bash.language()))


@dataclass(frozen=True, slots=True)
class ShellParseResult:
    """Syntax facts for one exact command, including its parsed AST root."""

    raw_command: str
    tokens: tuple[str, ...]
    root: ShellNode | None
    tokenization_succeeded: bool
    contains_shell_composition: bool
    contains_output_redirection: bool

    @property
    def executable_basename(self) -> str | None:
        """Return the basename of the first command the shell would run."""

        command = _first_simple_command(self.root)
        if command is None or command.executable is None:
            return None
        return Path(_strip_quotes(command.executable.text)).name


def parse_shell_ast(raw_command: str) -> ShellParseResult:
    """Parse one command into a Shell AST without executing or rewriting it.

    Args:
        raw_command (`str`):
            The exact command string to inspect.

    Returns:
        Syntax facts for the command. Malformed commands return a stable
        ``root=None`` result with conservative syntax facts so callers need
        no exception handling.
    """

    tokens, tokenization_succeeded = _tokenize(raw_command)
    root_node = _PARSER.parse(bytes(raw_command, "utf8")).root_node
    if not tokenization_succeeded or root_node.has_error:
        return ShellParseResult(
            raw_command=raw_command,
            tokens=tokens,
            root=None,
            tokenization_succeeded=False,
            contains_shell_composition=True,
            contains_output_redirection=_tokens_contain_output_redirection(tokens),
        )
    root = _convert_statement(root_node, raw_command)
    return ShellParseResult(
        raw_command=raw_command,
        tokens=tokens,
        root=root,
        tokenization_succeeded=True,
        contains_shell_composition=_contains_composition(root),
        contains_output_redirection=_contains_output_redirection(root),
    )


def _tokenize(raw_command: str) -> tuple[tuple[str, ...], bool]:
    """Split the command into quote-stripped tokens like the shell would."""

    try:
        tokens = shlex.split(raw_command, posix=True, comments=False)
    except ValueError:
        return (), False
    return tuple(tokens), True


def _tokens_contain_output_redirection(tokens: tuple[str, ...]) -> bool:
    """Detect file-writing redirections among tokens without a valid AST."""

    return any(
        ">&" not in token and _REDIRECTION_TOKEN_PATTERN.match(token)
        for token in tokens
    )


def _convert_statement(node: Node, text: str) -> ShellNode | None:
    """Convert one tree-sitter statement into the runtime AST model."""

    node_type = node.type
    if node_type in {"command", "declaration_command", "unset_command"}:
        return _convert_command(node, text)
    if node_type == "redirected_statement":
        return _convert_redirected(node, text)
    if node_type == "pipeline":
        return _convert_pipeline(node, text)
    if node_type in {"program", "list"}:
        return _sequence_from_elements(_collect_elements(node, text), _span(node))
    if node_type == "subshell":
        body = _sequence_from_elements(_collect_elements(node, text), _span(node))
        return Subshell(body, _span(node))
    if node_type == "variable_assignment":
        word = ShellWord(node.text.decode("utf8"), _span(node))
        return SimpleCommand(None, (word,), (), False, _span(node))
    return UnsupportedCommand(node_type, _span(node))


def _convert_command(node: Node, text: str) -> SimpleCommand:
    """Convert a simple, declaration or unset command node."""

    executable: ShellWord | None = None
    argv: list[ShellWord] = []
    redirects: list[ShellRedirect] = []
    has_command_substitution = False
    for child in node.children:
        child_type = child.type
        if child_type == "command_name" or child_type in _DECLARATION_KEYWORDS:
            executable = ShellWord(child.text.decode("utf8"), _span(child))
        elif child_type in _REDIRECT_NODES:
            redirects.append(_convert_redirect(child, text))
        elif child_type == "comment":
            continue
        else:
            argv.append(ShellWord(child.text.decode("utf8"), _span(child)))
            if not has_command_substitution and _contains_node_type(
                child, "command_substitution"
            ):
                has_command_substitution = True
    return SimpleCommand(
        executable,
        tuple(argv),
        tuple(redirects),
        has_command_substitution,
        _span(node),
    )


def _convert_redirected(node: Node, text: str) -> ShellNode:
    """Convert a redirected statement, merging redirects into simple commands."""

    inner: ShellNode | None = None
    redirects: list[ShellRedirect] = []
    for child in node.children:
        if child.type in _STATEMENT_NODES:
            inner = _convert_statement(child, text)
        elif child.type in _REDIRECT_NODES:
            redirects.append(_convert_redirect(child, text))
    redirect_tuple = tuple(redirects)
    if inner is None:
        return UnsupportedCommand("redirected_statement", _span(node))
    if isinstance(inner, SimpleCommand):
        return SimpleCommand(
            inner.executable,
            inner.argv,
            inner.redirects + redirect_tuple,
            inner.has_command_substitution,
            inner.span,
        )
    return RedirectedCommand(inner, redirect_tuple, _span(node))


def _convert_pipeline(node: Node, text: str) -> Pipeline:
    """Convert a pipeline node, keeping its statement order."""

    return Pipeline(
        tuple(
            _convert_statement(child, text)
            for child in node.children
            if child.type in _STATEMENT_NODES
        ),
        _span(node),
    )


def _collect_elements(node: Node, text: str) -> list[SequenceElement]:
    """Collect statements and their trailing separators from a container node."""

    elements: list[SequenceElement] = []
    for child in node.children:
        if child.type in _STATEMENT_NODES:
            elements.append(SequenceElement(_convert_statement(child, text), None))
        elif child.type in _SEPARATOR_NODES and elements:
            last = elements[-1]
            elements[-1] = SequenceElement(last.command, child.text.decode("utf8"))
    return elements


def _sequence_from_elements(
    elements: list[SequenceElement], span: ShellSpan
) -> ShellNode | None:
    """Unwrap single statements and wrap multiple ones into a Sequence."""

    if not elements:
        return None
    if len(elements) == 1 and elements[0].terminator is None:
        return elements[0].command
    return Sequence(tuple(elements), span)


def _convert_redirect(node: Node, text: str) -> ShellRedirect:
    """Convert a file, heredoc or here-string redirect node."""

    operator_parts: list[str] = []
    target: ShellWord | None = None
    for child in node.children:
        child_type = child.type
        if child_type == "file_descriptor" or child_type in _REDIRECT_OPERATOR_NODES:
            operator_parts.append(child.text.decode("utf8"))
        elif child_type in _WORD_NODES:
            target = ShellWord(child.text.decode("utf8"), _span(child))
    operator = "".join(operator_parts)
    return ShellRedirect(
        operator, target, ">" in operator and ">&" not in operator, _span(node)
    )


def _contains_composition(root: ShellNode | None) -> bool:
    """Return whether the AST is not one plain command without redirects."""

    if isinstance(root, SimpleCommand):
        return bool(root.redirects) or root.has_command_substitution
    return root is not None


def collect_redirects(root: ShellNode | None) -> tuple[ShellRedirect, ...]:
    """Collect every redirect in the AST in source order."""

    if isinstance(root, SimpleCommand):
        return root.redirects
    if isinstance(root, RedirectedCommand):
        return collect_redirects(root.command) + root.redirects
    if isinstance(root, Pipeline):
        return tuple(
            redirect
            for element in root.elements
            for redirect in collect_redirects(element)
        )
    if isinstance(root, Sequence):
        return tuple(
            redirect
            for element in root.elements
            for redirect in collect_redirects(element.command)
        )
    if isinstance(root, Subshell) and root.body is not None:
        return collect_redirects(root.body)
    return ()


def _contains_output_redirection(root: ShellNode | None) -> bool:
    """Return whether any redirect in the AST targets an output stream."""

    return any(redirect.is_output for redirect in collect_redirects(root))


def _first_simple_command(root: ShellNode | None) -> SimpleCommand | None:
    """Return the first atomic command in source order, if any."""

    if isinstance(root, SimpleCommand):
        return root
    if isinstance(root, RedirectedCommand):
        return _first_simple_command(root.command)
    if isinstance(root, Pipeline):
        for element in root.elements:
            found = _first_simple_command(element)
            if found is not None:
                return found
    elif isinstance(root, Sequence):
        for element in root.elements:
            found = _first_simple_command(element.command)
            if found is not None:
                return found
    elif isinstance(root, Subshell):
        return _first_simple_command(root.body)
    return None


def _contains_node_type(node: Node, target: str) -> bool:
    """Return whether the node subtree contains a node of the target type."""

    if node.type == target:
        return True
    return any(_contains_node_type(child, target) for child in node.children)


def _strip_quotes(text: str) -> str:
    """Strip one wrapping pair of single or double quotes from a token."""

    if len(text) >= 2 and text[0] in {"'", '"'} and text[-1] == text[0]:
        return text[1:-1]
    return text


def _span(node: Node) -> ShellSpan:
    """Convert a tree-sitter node byte range into a ShellSpan."""

    return ShellSpan(node.start_byte, node.end_byte)
