"""Unit tests for the Shell AST parser and its derived syntax facts."""

import pytest

from cli_agent.runtime._capability.command_parser import (
    FileRedirect,
    HereDocRedirect,
    HereStringRedirect,
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

    assert parsed.syntax_valid is True
    assert isinstance(parsed.root, SimpleCommand)
    assert parsed.root.executable.text == "ls"
    assert parsed.root.executable.span.start == 0
    assert parsed.root.executable.span.end == 2
    assert parsed.root.executable.value == "ls"
    assert parsed.root.executable.quote is None
    assert tuple(word.text for word in parsed.root.argv) == ("-la",)
    assert tuple((word.span.start, word.span.end) for word in parsed.root.argv) == (
        (3, 6),
    )


def test_word_spans_slice_unicode_source_and_expose_quote_facts() -> None:
    parsed = parse_shell_ast("echo 你好 'hi there'")
    root = parsed.root
    assert isinstance(root, SimpleCommand)

    word = root.argv[1]
    assert parsed.raw_command[word.span.start : word.span.end] == word.text
    assert root.argv[0].value == "你好"
    assert word.value == "hi there"
    assert word.quote == "single"
    assert word.quoted_content == "hi there"


def test_dynamic_words_have_no_static_value() -> None:
    parsed = parse_shell_ast('$COMMAND "$ARG"')

    assert isinstance(parsed.root, SimpleCommand)
    assert parsed.root.executable.value is None
    assert parsed.root.argv[0].value is None
    assert parsed.root.argv[0].quote == "double"
    assert parsed.root.argv[0].quoted_content == "$ARG"


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
    assert all(isinstance(redirect, FileRedirect) for redirect in parsed.root.redirects)
    assert [
        (r.operator, r.is_output, r.target.text) for r in parsed.root.redirects
    ] == [
        (">", True, "out.txt"),
        ("2>", True, "err.txt"),
    ]


def test_fd_duplication_is_not_output_file_write() -> None:
    parsed = parse_shell_ast("ls >/dev/null 2>&1")

    assert isinstance(parsed.root, SimpleCommand)
    assert all(isinstance(redirect, FileRedirect) for redirect in parsed.root.redirects)
    assert [(r.operator, r.is_output) for r in parsed.root.redirects] == [
        (">", True),
        ("2>&", False),
    ]


def test_heredoc_and_herestring_are_input_redirections() -> None:
    parsed = parse_shell_ast("cat <<'EOF'\nhello\nEOF")

    assert isinstance(parsed.root, SimpleCommand)
    heredoc = parsed.root.redirects[0]
    assert isinstance(heredoc, HereDocRedirect)
    assert heredoc.operator == "<<"
    assert heredoc.delimiter.value == "EOF"
    assert heredoc.delimiter.quote == "single"
    assert heredoc.body.text == "hello\n"
    assert heredoc.strip_tabs is False
    assert heredoc.expands is False

    parsed = parse_shell_ast("cmd <<< x")
    assert isinstance(parsed.root, SimpleCommand)
    herestring = parsed.root.redirects[0]
    assert isinstance(herestring, HereStringRedirect)
    assert herestring.operator == "<<<"
    assert herestring.value.value == "x"


def test_tab_stripping_and_unquoted_heredoc_facts_are_preserved() -> None:
    parsed = parse_shell_ast("cat <<-EOF\n\thello\n\tEOF")

    assert isinstance(parsed.root, SimpleCommand)
    heredoc = parsed.root.redirects[0]
    assert isinstance(heredoc, HereDocRedirect)
    assert heredoc.strip_tabs is True
    assert heredoc.expands is True
    assert heredoc.body.text == "\thello\n"


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
    assert parsed.root.prefix_assignments == ()
    assert parsed.root.executable.text == "export"
    assert tuple(word.text for word in parsed.root.argv) == ("A=1",)


def test_prefix_assignments_are_separate_from_command_arguments() -> None:
    parsed = parse_shell_ast("A=1 tools list")

    assert isinstance(parsed.root, SimpleCommand)
    assert tuple(word.value for word in parsed.root.prefix_assignments) == ("A=1",)
    assert parsed.root.executable.value == "tools"
    assert tuple(word.value for word in parsed.root.argv) == ("list",)
    assert parsed.command_head is None


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
    assert parsed.syntax_valid is False
    assert parsed.root is None
    assert parsed.contains_shell_composition is True


def test_failed_parse_keeps_conservative_syntax_facts() -> None:
    parsed = parse_shell_ast("echo hi >")

    assert parsed.syntax_valid is False
    assert parsed.contains_output_redirection is True


def test_empty_command_is_valid_but_empty() -> None:
    parsed = parse_shell_ast("")

    assert parsed.syntax_valid is True
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
        ("tools list", "tools"),
        ('"tools" list', "tools"),
        ("to\\ols list", "tools"),
        ("tools list | cat", "tools"),
        ("tools list; cat", "tools"),
        ("./tools list", "./tools"),
        ("env tools list", "env"),
        ("A=1 tools list", None),
        ("$COMMAND list", None),
        ("(tools list)", None),
        ('tools "unterminated', None),
    ),
)
def test_command_head_is_derived_from_top_level_static_syntax(
    raw: str,
    expected: str | None,
) -> None:
    assert parse_shell_ast(raw).command_head == expected


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
        ("tools run <<'PY'\nx\nPY", False),
    ),
)
def test_contains_output_redirection_is_derived_from_ast(
    raw: str, expected: bool
) -> None:
    assert parse_shell_ast(raw).contains_output_redirection is expected


def test_leading_arguments_are_derived_from_static_ast_words() -> None:
    parsed = parse_shell_ast("sed -n '5,10p' file.txt")

    assert parsed.leading_arguments == ("-n", "5,10p", "file.txt")


def test_collect_redirects_returns_flat_source_order() -> None:
    parsed = parse_shell_ast("echo hi > out.txt 2>err.txt")

    assert [r.target.text for r in collect_redirects(parsed.root)] == [
        "out.txt",
        "err.txt",
    ]

    parsed = parse_shell_ast("(ls) > out")
    assert [r.target.text for r in collect_redirects(parsed.root)] == ["out"]
