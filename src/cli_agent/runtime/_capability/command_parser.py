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
from typing import Literal

import tree_sitter_bash
from tree_sitter import Language, Node, Parser


@dataclass(frozen=True, slots=True)
class ShellSpan:
    """A character offset range into the original command text."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class ShellWord:
    """One shell word with its raw and statically knowable values."""

    text: str
    span: ShellSpan
    value: str | None
    quote: Literal["single", "double"] | None

    @property
    def quoted_content(self) -> str | None:
        """Return the exact content inside one wrapping quote pair."""

        if self.quote is None:
            return None
        return self.text[1:-1]


@dataclass(frozen=True, slots=True)
class ShellText:
    """One exact source fragment with its source span."""

    text: str
    span: ShellSpan


@dataclass(frozen=True, slots=True)
class FileRedirect:
    """One file or file-descriptor redirection."""

    operator: str
    target: ShellWord | None
    is_output: bool
    span: ShellSpan


@dataclass(frozen=True, slots=True)
class HereStringRedirect:
    """One inline word redirected to standard input."""

    operator: str
    value: ShellWord
    span: ShellSpan


@dataclass(frozen=True, slots=True)
class HereDocRedirect:
    """One multiline input block with its delimiter and exact body."""

    operator: str
    delimiter: ShellWord
    body: ShellText
    strip_tabs: bool
    expands: bool
    span: ShellSpan


ShellRedirect = FileRedirect | HereStringRedirect | HereDocRedirect


@dataclass(frozen=True, slots=True)
class SimpleCommand:
    """An atomic command: an executable, its arguments and attached redirects."""

    prefix_assignments: tuple[ShellWord, ...]
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
_DYNAMIC_WORD_NODES = frozenset(
    {
        "arithmetic_expansion",
        "command_substitution",
        "expansion",
        "process_substitution",
        "simple_expansion",
    }
)
_REDIRECTION_TOKEN_PATTERN = re.compile(r"\d*(?:>>?|<>|>\|)")

_PARSER = Parser(Language(tree_sitter_bash.language()))


@dataclass(frozen=True, slots=True)
class _Source:
    data: bytes
    character_offsets: tuple[int, ...]

    @classmethod
    def from_text(cls, text: str) -> _Source:
        data = text.encode("utf8")
        offsets = [0]
        for index, character in enumerate(text, start=1):
            offsets.extend([index] * len(character.encode("utf8")))
        return cls(data, tuple(offsets))

    def node_text(self, node: Node) -> str:
        return self.data[node.start_byte : node.end_byte].decode("utf8")

    def range_text(self, start_byte: int, end_byte: int) -> str:
        return self.data[start_byte:end_byte].decode("utf8")

    def span(self, node: Node) -> ShellSpan:
        return self.range_span(node.start_byte, node.end_byte)

    def range_span(self, start_byte: int, end_byte: int) -> ShellSpan:
        return ShellSpan(
            self.character_offsets[start_byte],
            self.character_offsets[end_byte],
        )


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
    def leading_command(self) -> SimpleCommand | None:
        """Return the first command at the current shell nesting level."""

        return _leading_top_level_command(self.root)

    @property
    def command_head(self) -> str | None:
        """Return a statically known Runtime custom-command head."""

        command = self.leading_command
        if command is None or command.prefix_assignments or command.executable is None:
            return None
        return command.executable.value

    @property
    def executable_basename(self) -> str | None:
        """Return the basename of the first command the shell would run."""

        command = _first_simple_command(self.root)
        if command is None or command.executable is None:
            return None
        value = command.executable.value
        return None if value is None else Path(value).name


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
    source = _Source.from_text(raw_command)
    root_node = _PARSER.parse(source.data).root_node
    if not tokenization_succeeded or root_node.has_error:
        return ShellParseResult(
            raw_command=raw_command,
            tokens=tokens,
            root=None,
            tokenization_succeeded=False,
            contains_shell_composition=True,
            contains_output_redirection=_tokens_contain_output_redirection(tokens),
        )
    root = _convert_statement(root_node, source)
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


def _convert_statement(node: Node, source: _Source) -> ShellNode | None:
    """Convert one tree-sitter statement into the runtime AST model."""

    node_type = node.type
    if node_type in {"command", "declaration_command", "unset_command"}:
        return _convert_command(node, source)
    if node_type == "redirected_statement":
        return _convert_redirected(node, source)
    if node_type == "pipeline":
        return _convert_pipeline(node, source)
    if node_type in {"program", "list"}:
        return _sequence_from_elements(
            _collect_elements(node, source), source.span(node)
        )
    if node_type == "subshell":
        body = _sequence_from_elements(
            _collect_elements(node, source), source.span(node)
        )
        return Subshell(body, source.span(node))
    if node_type == "variable_assignment":
        word = _convert_word(node, source)
        return SimpleCommand((), None, (word,), (), False, source.span(node))
    return UnsupportedCommand(node_type, source.span(node))


def _convert_command(node: Node, source: _Source) -> SimpleCommand:
    """Convert a simple, declaration or unset command node."""

    prefix_assignments: list[ShellWord] = []
    executable: ShellWord | None = None
    argv: list[ShellWord] = []
    redirects: list[ShellRedirect] = []
    for child in node.children:
        child_type = child.type
        if child_type == "command_name" or child_type in _DECLARATION_KEYWORDS:
            executable = _convert_word(child, source)
        elif child_type in _REDIRECT_NODES:
            redirects.extend(_convert_redirects(child, source))
        elif child_type == "comment":
            continue
        elif (
            node.type == "command"
            and executable is None
            and child_type in {"environment_assignment", "variable_assignment"}
        ):
            prefix_assignments.append(_convert_word(child, source))
        else:
            argv.append(_convert_word(child, source))
    return SimpleCommand(
        tuple(prefix_assignments),
        executable,
        tuple(argv),
        tuple(redirects),
        _contains_node_type(node, "command_substitution"),
        source.span(node),
    )


def _convert_redirected(node: Node, source: _Source) -> ShellNode:
    """Convert a redirected statement, merging redirects into simple commands."""

    inner: ShellNode | None = None
    redirects: list[ShellRedirect] = []
    for child in node.children:
        if child.type in _STATEMENT_NODES:
            inner = _convert_statement(child, source)
        elif child.type in _REDIRECT_NODES:
            redirects.extend(_convert_redirects(child, source))
    redirect_tuple = tuple(redirects)
    if inner is None:
        return UnsupportedCommand("redirected_statement", source.span(node))
    if isinstance(inner, SimpleCommand):
        return SimpleCommand(
            inner.prefix_assignments,
            inner.executable,
            inner.argv,
            inner.redirects + redirect_tuple,
            inner.has_command_substitution,
            inner.span,
        )
    return RedirectedCommand(inner, redirect_tuple, source.span(node))


def _convert_pipeline(node: Node, source: _Source) -> Pipeline:
    """Convert a pipeline node, keeping its statement order."""

    return Pipeline(
        tuple(
            _convert_required_statement(child, source)
            for child in node.children
            if child.type in _STATEMENT_NODES
        ),
        source.span(node),
    )


def _collect_elements(node: Node, source: _Source) -> list[SequenceElement]:
    """Collect statements and their trailing separators from a container node."""

    elements: list[SequenceElement] = []
    for child in node.children:
        if child.type in _STATEMENT_NODES:
            elements.append(
                SequenceElement(_convert_required_statement(child, source), None)
            )
        elif child.type in _SEPARATOR_NODES and elements:
            last = elements[-1]
            elements[-1] = SequenceElement(last.command, source.node_text(child))
    return elements


def _convert_required_statement(node: Node, source: _Source) -> ShellNode:
    """Convert a child known by its parent to be one statement."""

    statement = _convert_statement(node, source)
    if statement is None:
        return UnsupportedCommand(node.type, source.span(node))
    return statement


def _sequence_from_elements(
    elements: list[SequenceElement], span: ShellSpan
) -> ShellNode | None:
    """Unwrap single statements and wrap multiple ones into a Sequence."""

    if not elements:
        return None
    if len(elements) == 1 and elements[0].terminator is None:
        return elements[0].command
    return Sequence(tuple(elements), span)


def _convert_redirects(node: Node, source: _Source) -> tuple[ShellRedirect, ...]:
    """Convert one redirect node and any redirects nested by Bash grammar."""

    if node.type == "heredoc_redirect":
        return (
            _convert_heredoc(node, source),
            *(
                redirect
                for child in node.children
                if child.type in _REDIRECT_NODES
                for redirect in _convert_redirects(child, source)
            ),
        )
    if node.type == "herestring_redirect":
        return (_convert_herestring(node, source),)

    operator_parts: list[str] = []
    target: ShellWord | None = None
    for child in node.children:
        child_type = child.type
        if child_type == "file_descriptor" or child_type in _REDIRECT_OPERATOR_NODES:
            operator_parts.append(source.node_text(child))
        elif child_type in _WORD_NODES:
            target = _convert_word(child, source)
    operator = "".join(operator_parts)
    return (
        FileRedirect(
            operator,
            target,
            ">" in operator and ">&" not in operator,
            source.span(node),
        ),
    )


def _convert_heredoc(node: Node, source: _Source) -> HereDocRedirect:
    """Convert one parsed heredoc without discarding its body."""

    delimiter_node = next(
        child for child in node.children if child.type == "heredoc_start"
    )
    end_node = next(child for child in node.children if child.type == "heredoc_end")
    delimiter = _convert_word(delimiter_node, source)
    operator = _redirect_operator(node, source)
    opening_line_end = source.data.find(b"\n", delimiter_node.end_byte)
    body_start = opening_line_end + 1
    closing_line_start = source.data.rfind(b"\n", body_start, end_node.start_byte)
    body_end = body_start if closing_line_start < body_start else closing_line_start + 1
    return HereDocRedirect(
        operator=operator,
        delimiter=delimiter,
        body=ShellText(
            source.range_text(body_start, body_end),
            source.range_span(body_start, body_end),
        ),
        strip_tabs=operator.endswith("<<-"),
        expands=not any(character in delimiter.text for character in "'\"\\"),
        span=source.span(node),
    )


def _convert_herestring(node: Node, source: _Source) -> HereStringRedirect:
    """Convert one parsed here-string input redirect."""

    value_node = next(child for child in node.children if child.type in _WORD_NODES)
    return HereStringRedirect(
        operator=_redirect_operator(node, source),
        value=_convert_word(value_node, source),
        span=source.span(node),
    )


def _redirect_operator(node: Node, source: _Source) -> str:
    """Return one redirect operator including an optional descriptor."""

    return "".join(
        source.node_text(child)
        for child in node.children
        if child.type == "file_descriptor" or child.type in _REDIRECT_OPERATOR_NODES
    )


def _convert_word(node: Node, source: _Source) -> ShellWord:
    """Convert one word-like node into exact and static forms."""

    text = source.node_text(node)
    return ShellWord(
        text=text,
        span=source.span(node),
        value=_static_word_value(node, text),
        quote=_word_quote(node, text),
    )


def _static_word_value(node: Node, text: str) -> str | None:
    """Decode one word whose value needs no runtime Shell expansion."""

    if any(_contains_node_type(node, node_type) for node_type in _DYNAMIC_WORD_NODES):
        return None
    try:
        values = shlex.split(text, comments=False, posix=True)
    except ValueError:
        return None
    return values[0] if len(values) == 1 else None


def _word_quote(
    node: Node,
    text: str,
) -> Literal["single", "double"] | None:
    """Classify a word fully wrapped by one ordinary quote pair."""

    effective = node
    if node.type == "command_name" and len(node.named_children) == 1:
        effective = node.named_children[0]
    if effective.type == "raw_string":
        return "single"
    if effective.type == "string":
        return "double"
    if len(text) >= 2 and text[0] == text[-1] == "'":
        return "single"
    if len(text) >= 2 and text[0] == text[-1] == '"':
        return "double"
    return None


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

    return any(
        isinstance(redirect, FileRedirect) and redirect.is_output
        for redirect in collect_redirects(root)
    )


def _leading_top_level_command(root: ShellNode | None) -> SimpleCommand | None:
    """Return the first command without descending into nested shell scopes."""

    if isinstance(root, SimpleCommand):
        return root
    if isinstance(root, Pipeline) and root.elements:
        return _leading_top_level_command(root.elements[0])
    if isinstance(root, Sequence) and root.elements:
        return _leading_top_level_command(root.elements[0].command)
    return None


def _first_simple_command(root: ShellNode | None) -> SimpleCommand | None:
    """Return the first atomic command in source order, if any."""

    if isinstance(root, SimpleCommand):
        return root
    if isinstance(root, RedirectedCommand):
        return _first_simple_command(root.command)
    if isinstance(root, Pipeline):
        for pipeline_element in root.elements:
            found = _first_simple_command(pipeline_element)
            if found is not None:
                return found
    elif isinstance(root, Sequence):
        for sequence_element in root.elements:
            found = _first_simple_command(sequence_element.command)
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
