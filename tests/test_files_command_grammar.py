"""Unit tests for the reserved Files command grammar."""

import pytest

from cli_agent.runtime._backend import _FileEdit
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment.handlers.files import (
    FileCommand,
    parse_edit_payload,
    parse_files_command,
)


def test_write_returns_path_without_payload() -> None:
    facts = parse_files_command(parse_shell_ast("files write notes.txt"))

    assert facts is not None
    assert facts.operation == "write"
    assert facts.valid is True
    assert facts.validation_error is None
    assert facts.path == "notes.txt"
    assert not hasattr(facts, "content")


def test_edit_returns_path_without_payload() -> None:
    facts = parse_files_command(parse_shell_ast("files edit main.py"))

    assert facts is not None
    assert facts.operation == "edit"
    assert facts.valid is True
    assert facts.path == "main.py"
    assert not hasattr(facts, "edits")


def test_write_preserves_quoted_path_with_spaces() -> None:
    facts = parse_files_command(parse_shell_ast("files write 'my file.py'"))

    assert facts is not None
    assert facts.valid is True
    assert facts.path == "my file.py"


def test_edit_accepts_quoted_path() -> None:
    facts = parse_files_command(parse_shell_ast("files edit \"main file.py\""))

    assert facts is not None
    assert facts.valid is True
    assert facts.path == "main file.py"


@pytest.mark.parametrize(
    "raw",
    (
        "files",
        "files list",
        "files nonsense main.py",
        "files write",
        "files write f extra",
        "files edit f extra",
        "files write \"$VAR\"",
        "files write f <<'EOF'\nline\nEOF",
        "files write f <<EOF\nline\nEOF",
        'files write f <<"EOF"\nline\nEOF',
        "files write f <<'EDI'\nline\nEDI",
        "files edit f <<'EDI'\nline\nEDI",
        "files edit f <<'EOF'\nline\nEOF",
        "files edit f <<-'EOF'\n\tline\n\tEOF",
        "files write f <<< 'EOF'\nline\nEOF",
        "files write f > out.txt",
        "files write f <<'EOF' > out.txt\nline\nEOF",
        "files edit f '{\"edits\": []}'",
        "files edit f \"{\\\"edits\\\": []}\"",
        "files write f | cat",
        "files write f && echo done",
        "files write f || echo done",
        "files write f; echo done",
        "files write f\nline\nEOF",
        "files edit f --flag",
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
        ("files write \"$VAR\"", "statically known"),
        ("files write f extra", "exactly one path"),
        ("files write f <<'EOF'\nx\nEOF", "exec stdin"),
        ("files write f <<'EDI'\nx\nEDI", "exec stdin"),
        ("files edit f <<'EOF'\nx\nEOF", "exec stdin"),
        ("files edit f '{\"edits\": []}'", "quoted JSON"),
        ("files write f > out.txt", "redirects"),
        ("files write f | cat", "Usage: files"),
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


def test_parse_edit_payload_returns_edits_tuple() -> None:
    payload = (
        '{"edits": [{"oldText": "one", "newText": "two"}, '
        '{"oldText": "three", "newText": "four"}]}'
    )

    assert parse_edit_payload(payload) == (
        _FileEdit(old_text="one", new_text="two"),
        _FileEdit(old_text="three", new_text="four"),
    )


def test_parse_edit_payload_allows_empty_new_text() -> None:
    assert parse_edit_payload('{"edits": [{"oldText": "remove me", "newText": ""}]}') == (
        _FileEdit(old_text="remove me", new_text=""),
    )


@pytest.mark.parametrize(
    ("payload", "reason"),
    (
        ("not json", "not valid JSON"),
        ("[]", "JSON object with an edits array"),
        ('{"edits": "empty"}', "JSON object with an edits array"),
        ('{"edits": []}', "must not be empty"),
        ('{"edits": [{}]}', "non-empty oldText"),
        ('{"edits": [{"oldText": "", "newText": "b"}]}', "non-empty oldText"),
        ('{"edits": [{"oldText": "a"}]}', "requires a string newText"),
        ('{"edits": [{"oldText": 7, "newText": "b"}]}', "non-empty oldText"),
        ('{"edits": [{"oldText": "a", "newText": 7}]}', "requires a string newText"),
        ('{"edits": ["x"]}', "must be a JSON object"),
        ('{"edits": [{"oldText": "a", "newText": "b"}, {}]}', r"edits\[2\]"),
    ),
)
def test_edit_payload_validation_reports_each_failure(
    payload: str,
    reason: str,
) -> None:
    with pytest.raises(ValueError, match=reason):
        parse_edit_payload(payload)


@pytest.mark.parametrize(
    "raw",
    (
        "write f",
        "env files write f",
        "./files write f",
        "$COMMAND write f",
        "A=1 files write f",
        "(files write f)",
        "tool write f",
        "echo files write f",
    ),
)
def test_non_files_heads_are_not_reserved(raw: str) -> None:
    facts = parse_files_command(parse_shell_ast(raw))

    assert facts is None


def test_files_grammar_returns_facts_not_policy_state() -> None:
    facts = parse_files_command(parse_shell_ast("files write f"))

    assert isinstance(facts, FileCommand)
    assert facts.operation == "write"
