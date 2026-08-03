"""Unit tests for the Shell AST parser and its derived syntax facts."""

import pytest

from cli_agent.runtime._capability.command_parser import (
    Pipeline,
    RedirectedCommand,
    Sequence,
    ShellParseResult,
    SimpleCommand,
    Subshell,
    UnsupportedCommand,
    collect_redirects,
    parse_shell_ast,
)


def test_simple_command_exposes_executable_and_argv() -> None:
    parsed = parse_shell_ast("ls -la")

    assert parsed.tokenization_succeeded is True
    assert isinstance(parsed.root, SimpleCommand)
    assert parsed.root.executable.text == "ls"
    assert parsed.root.executable.span.start == 0
    assert parsed.root.executable.span.end == 2
    assert tuple(word.text for word in parsed.root.argv) == ("-la",)
    assert tuple((word.span.start, word.span.end) for word in parsed.root.argv) == (
        (3, 6),
    )


def test_word_spans_slice_back_into_raw_command() -> None:
    parsed = parse_shell_ast("echo 'hi there'")
    root = parsed.root
    assert isinstance(root, SimpleCommand)

    word = root.argv[0]
    assert parsed.raw_command[word.span.start : word.span.end] == word.text


def test_pipeline_expresses_ordered_elements() -> None:
    parsed = parse_shell_ast("cat a.txt | rg foo")

    assert isinstance(parsed.root, Pipeline)
    assert [element.executable.text for element in parsed.root.elements] == [
        "cat",
        "rg",
    ]


def test_sequence_expresses_separators_and_background() -> None:
    parsed = parse_shell_ast("echo hi; echo there &")

    assert isinstance(parsed.root, Sequence)
    assert [element.terminator for element in parsed.root.elements] == [";", "&"]
    assert [element.command.executable.text for element in parsed.root.elements] == [
        "echo",
        "echo",
    ]


def test_background_only_command_keeps_sequence_wrapper() -> None:
    parsed = parse_shell_ast("sleep 5 &")

    assert isinstance(parsed.root, Sequence)
    assert parsed.root.elements[0].terminator == "&"


def test_nested_and_or_is_preserved() -> None:
    parsed = parse_shell_ast("git status && echo ok || echo fail")

    assert isinstance(parsed.root, Sequence)
    inner, outer_separator = (
        parsed.root.elements[0].command,
        parsed.root.elements[0].terminator,
    )
    assert outer_separator == "||"
    assert isinstance(inner, Sequence)
    assert [element.terminator for element in inner.elements] == ["&&", None]


def test_redirections_classify_output_and_input_targets() -> None:
    parsed = parse_shell_ast("echo hi > out.txt 2>err.txt")

    assert isinstance(parsed.root, SimpleCommand)
    assert [
        (r.operator, r.is_output, r.target.text) for r in parsed.root.redirects
    ] == [
        (">", True, "out.txt"),
        ("2>", True, "err.txt"),
    ]


def test_fd_duplication_is_not_output_file_write() -> None:
    parsed = parse_shell_ast("ls >/dev/null 2>&1")

    assert isinstance(parsed.root, SimpleCommand)
    assert [(r.operator, r.is_output) for r in parsed.root.redirects] == [
        (">", True),
        ("2>&", False),
    ]


def test_heredoc_and_herestring_are_input_redirections() -> None:
    parsed = parse_shell_ast("cat <<EOF\nhello\nEOF")

    assert isinstance(parsed.root, SimpleCommand)
    assert parsed.root.redirects[0].operator == "<<"
    assert parsed.root.redirects[0].target.text == "EOF"
    assert parsed.root.redirects[0].is_output is False

    parsed = parse_shell_ast("cmd <<< x")
    assert parsed.root.redirects[0].operator == "<<<"
    assert parsed.root.redirects[0].is_output is False


def test_command_substitution_is_flagged() -> None:
    for raw in ("echo $(date)", "echo `date`", 'echo "$(date)"'):
        parsed = parse_shell_ast(raw)
        assert isinstance(parsed.root, SimpleCommand)
        assert parsed.root.has_command_substitution is True


def test_subshell_expresses_body() -> None:
    parsed = parse_shell_ast("(cd /tmp && pwd)")

    assert isinstance(parsed.root, Subshell)
    assert isinstance(parsed.root.body, Sequence)


def test_redirected_simple_command_merges_redirects() -> None:
    parsed = parse_shell_ast("cat > out.txt")

    assert isinstance(parsed.root, SimpleCommand)
    assert len(parsed.root.redirects) == 1


def test_redirected_complex_statement_wraps() -> None:
    parsed = parse_shell_ast("(ls) > out")

    assert isinstance(parsed.root, RedirectedCommand)
    assert isinstance(parsed.root.command, Subshell)
    assert [r.target.text for r in parsed.root.redirects] == ["out"]


def test_declaration_command_keeps_executable() -> None:
    parsed = parse_shell_ast("export A=1")

    assert isinstance(parsed.root, SimpleCommand)
    assert parsed.root.executable.text == "export"
    assert tuple(word.text for word in parsed.root.argv) == ("A=1",)


def test_unsupported_statements_stay_conservative() -> None:
    parsed = parse_shell_ast("if true; then ls; fi")

    assert isinstance(parsed.root, UnsupportedCommand)
    assert parsed.root.node_type == "if_statement"

    parsed = parse_shell_ast("! ls")
    assert isinstance(parsed.root, UnsupportedCommand)
    assert parsed.root.node_type == "negated_command"


@pytest.mark.parametrize(
    "raw",
    (
        'echo "unterminated',
        "cat file |",
        "&& echo hi",
        "echo hi >",
        "(echo hi",
        "cmd \\",
        "&",
        "echo `",
        "echo 'x",
    ),
)
def test_malformed_commands_fail_closed(raw: str) -> None:
    parsed = parse_shell_ast(raw)

    assert isinstance(parsed, ShellParseResult)
    assert parsed.tokenization_succeeded is False
    assert parsed.root is None
    assert parsed.contains_shell_composition is True


def test_failed_parse_keeps_tokens_and_detects_redirection() -> None:
    parsed = parse_shell_ast("echo hi >")

    assert parsed.tokenization_succeeded is False
    assert parsed.tokens == ("echo", "hi", ">")
    assert parsed.contains_output_redirection is True


def test_empty_command_is_valid_but_empty() -> None:
    parsed = parse_shell_ast("")

    assert parsed.tokenization_succeeded is True
    assert parsed.root is None
    assert parsed.executable_basename is None
    assert parsed.contains_shell_composition is False
    assert parsed.contains_output_redirection is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("ls -la", "ls"),
        ("cd /tmp", "cd"),
        ("/usr/bin/ls -la", "ls"),
        ("'echo' hi", "echo"),
        ("cat a.txt | rg foo", "cat"),
        ("FOO=bar cmd arg", "cmd"),
        ("export A=1", "export"),
        ("git status && echo ok", "git"),
        ("(cd /tmp && pwd)", "cd"),
        ("FOO=bar", None),
        ("! ls", None),
        ("", None),
    ),
)
def test_executable_basename_is_derived_from_ast(
    raw: str, expected: str | None
) -> None:
    assert parse_shell_ast(raw).executable_basename == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("echo hi", False),
        ("echo hi > out.txt", True),
        ("echo hi; echo there", True),
        ("cat a.txt | rg foo", True),
        ("echo $(date)", True),
        ("sleep 5 &", True),
        ("(pwd)", True),
        ("FOO=bar", False),
        ("sed -n 1p file.txt", False),
        ("git status", False),
    ),
)
def test_contains_shell_composition_is_derived_from_ast(
    raw: str, expected: bool
) -> None:
    assert parse_shell_ast(raw).contains_shell_composition is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    (
        ("echo hi", False),
        ("echo hi > out.txt", True),
        ("echo hi >> out.txt", True),
        ("echo hi >| out.txt", True),
        ("cat < in.txt", False),
        ("echo value 2>&1", False),
        ("ls >/dev/null 2>&1", True),
        ("cat <<EOF\nhello\nEOF", False),
        ("echo hi > out.txt 2>err.txt", True),
        ("tools run PY<<\nx\nPY", False),
    ),
)
def test_contains_output_redirection_is_derived_from_ast(
    raw: str, expected: bool
) -> None:
    assert parse_shell_ast(raw).contains_output_redirection is expected


def test_tokens_stay_quote_stripped_for_existing_consumers() -> None:
    parsed = parse_shell_ast("sed -n '5,10p' file.txt")

    assert parsed.tokens == ("sed", "-n", "5,10p", "file.txt")


def test_collect_redirects_returns_flat_source_order() -> None:
    parsed = parse_shell_ast("echo hi > out.txt 2>err.txt")

    assert [r.target.text for r in collect_redirects(parsed.root)] == [
        "out.txt",
        "err.txt",
    ]

    parsed = parse_shell_ast("(ls) > out")
    assert [r.target.text for r in collect_redirects(parsed.root)] == ["out"]
