"""Unit tests for the reserved Files command grammar."""

import pytest

from cli_agent.runtime._backend import _FileEdit
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment.handlers.files import (
    FileCommand,
    parse_files_command,
)


def test_write_heredoc_returns_path_and_raw_content() -> None:
    command = parse_shell_ast("files write notes.txt <<'EOF'\nline1\nline2\nEOF")

    facts = parse_files_command(command)

    assert facts is not None
    assert facts.operation == "write"
    assert facts.valid is True
    assert facts.validation_error is None
    assert facts.path == "notes.txt"
    assert facts.content == "line1\nline2\n"


@pytest.mark.parametrize(
    "raw",
    (
        "files write notes.txt <<EOF\nline\nEOF",
        "files write notes.txt <<'EOF'\nline\nEOF",
        'files write notes.txt <<"EOF"\nline\nEOF',
    ),
)
def test_write_accepts_all_three_unquoted_delimiter_spellings(
    raw: str,
) -> None:
    facts = parse_files_command(parse_shell_ast(raw))

    assert facts is not None
    assert facts.valid is True
    assert facts.operation == "write"
    assert facts.path == "notes.txt"
    assert facts.content == "line\n"


def test_write_preserves_quoted_path_with_spaces() -> None:
    facts = parse_files_command(
        parse_shell_ast("files write 'my file.py' <<'EOF'\ncode\nEOF")
    )

    assert facts is not None
    assert facts.valid is True
    assert facts.path == "my file.py"
    assert facts.content == "code\n"


def test_write_allows_empty_content() -> None:
    facts = parse_files_command(parse_shell_ast("files write f <<'EOF'\nEOF"))

    assert facts is not None
    assert facts.valid is True
    assert facts.operation == "write"
    assert facts.content == ""


def test_write_terminates_at_first_standalone_eof_line() -> None:
    facts = parse_files_command(
        parse_shell_ast("files write f <<'EOF'\nbefore\nEOF\nEOF")
    )

    assert facts is not None
    assert facts.valid is False
    assert "EOF" in (facts.validation_error or "")


def test_edit_heredoc_returns_parsed_edits() -> None:
    command = parse_shell_ast(
        "files edit main.py <<'EDI'\n"
        '{"edits": [{"oldText": "one", "newText": "two"}, '
        '{"oldText": "three", "newText": "four"}]}\n'
        "EDI"
    )

    facts = parse_files_command(command)

    assert facts is not None
    assert facts.operation == "edit"
    assert facts.valid is True
    assert facts.path == "main.py"
    assert facts.edits == (
        _FileEdit(old_text="one", new_text="two"),
        _FileEdit(old_text="three", new_text="four"),
    )


def test_edit_accepts_single_line_quoted_payload() -> None:
    facts = parse_files_command(
        parse_shell_ast(
            'files edit main.py \'{"edits": [{"oldText": "a", "newText": "b"}]}\''
        )
    )

    assert facts is not None
    assert facts.valid is True
    assert facts.operation == "edit"
    assert facts.path == "main.py"
    assert facts.edits == (_FileEdit(old_text="a", new_text="b"),)


def test_edit_ignores_extra_json_keys() -> None:
    facts = parse_files_command(
        parse_shell_ast(
            "files edit main.py <<'EDI'\n"
            '{"note": "x", "edits": [{"oldText": "a", "newText": "b"}]}\n'
            "EDI"
        )
    )

    assert facts is not None
    assert facts.valid is True
    assert facts.edits == (_FileEdit(old_text="a", new_text="b"),)


@pytest.mark.parametrize(
    "raw",
    (
        "files",
        "files list",
        "files nonsense main.py <<'EOF'\nline\nEOF",
        "files write",
        "files write f",
        "files write f extra <<'EOF'\nline\nEOF",
        "files edit f",
        "files write f <<'EDI'\nline\nEDI",
        "files edit f <<'EOF'\nline\nEOF",
        "files write f <<-'EOF'\n\tline\n\tEOF",
        "files write f <<< 'EOF'\nline\nEOF",
        "files write f <<'EOF' > out.txt\nline\nEOF",
        "files write \"$VAR\" <<'EOF'\nline\nEOF",
        "files write f | cat",
        "files write f <<'EOF'\nline\nEOF && echo done",
        "files write f <<'EOF'\nline\nEOF\ngarbage",
        "files write f\nline\nEOF",
        "files write f <<'WRONG'\nline\nWRONG",
    ),
)
def test_invalid_files_shapes_return_usage_errors(raw: str) -> None:
    facts = parse_files_command(parse_shell_ast(raw))

    assert facts is not None
    assert facts.operation == "invalid"
    assert facts.valid is False
    assert facts.validation_error is not None


@pytest.mark.parametrize(
    ("raw", "reason"),
    (
        ("files", "Usage: files"),
        ("files nonsense f", "unknown files subcommand: nonsense"),
        ("files write \"$VAR\" <<'EOF'\nx\nEOF", "statically known"),
        ("files write f", "heredoc"),
        ("files write f <<'EDI'\nx\nEDI", "<<'EOF'"),
        ("files edit f <<'EOF'\nx\nEOF", "<<'EDI'"),
    ),
)
def test_invalid_files_shapes_report_specific_reasons(
    raw: str,
    reason: str,
) -> None:
    facts = parse_files_command(parse_shell_ast(raw))

    assert facts is not None
    assert facts.valid is False
    assert reason in (facts.validation_error or "")


@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        ("not json", "not valid JSON"),
        ("[]", "JSON object with an edits array"),
        ('{"edits": "empty"}', "JSON object with an edits array"),
        ('{"edits": []}', "must not be empty"),
        ('{"edits": [{}]}', "non-empty oldText"),
        ('{"edits": [{"oldText": "", "newText": "b"}]}', "non-empty oldText"),
        ('{"edits": [{"oldText": "a"}]}', "non-empty newText"),
        ('{"edits": [{"oldText": "a", "newText": ""}]}', "non-empty newText"),
        ('{"edits": [{"oldText": 7, "newText": "b"}]}', "non-empty oldText"),
        ('{"edits": ["x"]}', "must be a JSON object"),
        ('{"edits": [{"oldText": "a", "newText": "b"}, {}]}', "edits[2]"),
    ),
)
def test_edit_payload_validation_reports_each_failure(
    payload: str, reason: str
) -> None:
    command = parse_shell_ast(f"files edit main.py <<'EDI'\n{payload}\nEDI")

    facts = parse_files_command(command)

    assert facts is not None
    assert facts.valid is False
    assert facts.operation == "invalid"
    assert reason in (facts.validation_error or "")


@pytest.mark.parametrize(
    "raw",
    (
        "write f <<'EOF'\nline\nEOF",
        "env files write f <<'EOF'\nline\nEOF",
        "./files write f <<'EOF'\nline\nEOF",
        "$COMMAND write f <<'EOF'\nline\nEOF",
        "A=1 files write f <<'EOF'\nline\nEOF",
        "tool write f",
        "echo files write f",
    ),
)
def test_non_files_heads_are_not_reserved(raw: str) -> None:
    facts = parse_files_command(parse_shell_ast(raw))

    assert facts is None


def test_files_grammar_returns_facts_not_policy_state() -> None:
    facts = parse_files_command(parse_shell_ast("files write f <<'EOF'\nx\nEOF"))

    assert isinstance(facts, FileCommand)
    assert facts.operation == "write"
