"""Unit tests for the files edit Runtime command handler."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cli_agent.runtime._backend import _FileEdit
from cli_agent.runtime._backend.edit import apply_edits
from cli_agent.runtime._backend.local import _LocalBackendWorkspace
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._capability.view import _CapabilityView
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionOutcome,
)
from cli_agent.runtime._environment.handlers.files import _FileHandler


def test_apply_edits_replaces_one_exact_block() -> None:
    assert apply_edits("hello world\n", (_FileEdit("world", "there"),), "f") == (
        "hello there\n"
    )


def test_apply_edits_replaces_multiple_disjoint_blocks() -> None:
    content = "one two three four"
    edits = (_FileEdit("two", "2"), _FileEdit("four", "4"))

    assert apply_edits(content, edits, "f") == "one 2 three 4"


def test_apply_edits_applies_reverse_so_offsets_stay_stable() -> None:
    edits = (_FileEdit("x", "abc"), _FileEdit("y", "z"))

    assert apply_edits("xy", edits, "f") == "abcz"


def test_apply_edits_matches_against_original_content_only() -> None:
    edits = (_FileEdit("aaa", "ccc"), _FileEdit("bbb", "aaa"))

    assert apply_edits("aaabbb", edits, "f") == "cccaaa"


def test_apply_edits_accepts_crlf_old_text_against_lf_content() -> None:
    assert apply_edits("a\nb", (_FileEdit("a\r\nb", "c"),), "f") == "c"


@pytest.mark.parametrize(
    ("content", "edits", "message"),
    (
        ("x", (_FileEdit("", "y"),), "oldText must not be empty in f."),
        ("hello", (_FileEdit("missing", "x"),), "Could not find the exact text in f."),
        ("aa", (_FileEdit("a", "b"),), "Found 2 occurrences of the text in f."),
        ("abc", (_FileEdit("ab", "x"), _FileEdit("bc", "y")), "overlap in f."),
        ("x", (_FileEdit("x", "x"),), "No changes made to f."),
    ),
)
def test_apply_edits_reports_each_rejection(
    content: str,
    edits: tuple[_FileEdit, ...],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        apply_edits(content, edits, "f")


def test_apply_edits_reports_multi_edit_rejections_with_index() -> None:
    with pytest.raises(ValueError, match="Could not find edits\\[1\\]"):
        apply_edits("abc", (_FileEdit("a", "x"), _FileEdit("z", "y")), "f")


def test_files_edit_replaces_a_single_block(tmp_path: Path) -> None:
    target = tmp_path / "main.txt"
    target.write_text("hello world\n", encoding="utf-8")

    outcome, output = _run(
        tmp_path,
        "files edit main.txt <<'EDI'\n"
        '{"edits": [{"oldText": "world", "newText": "there"}]}\n'
        "EDI",
    )

    assert outcome == _ExecutionOutcome.exited()
    assert target.read_text(encoding="utf-8") == "hello there\n"
    assert output.text("stdout") == f"replaced 1 block(s) in {target}\n"
    assert output.text("stderr") == ""


def test_files_edit_replaces_multiple_blocks_in_one_call(tmp_path: Path) -> None:
    target = tmp_path / "main.txt"
    target.write_text("alpha beta gamma\n", encoding="utf-8")

    outcome, output = _run(
        tmp_path,
        "files edit main.txt <<'EDI'\n"
        '{"edits": [{"oldText": "alpha", "newText": "1"}, '
        '{"oldText": "gamma", "newText": "3"}]}\n'
        "EDI",
    )

    assert outcome == _ExecutionOutcome.exited()
    assert target.read_text(encoding="utf-8") == "1 beta 3\n"
    assert output.text("stdout") == f"replaced 2 block(s) in {target}\n"


def test_files_edit_accepts_quoted_payload_form(tmp_path: Path) -> None:
    target = tmp_path / "main.txt"
    target.write_text("abc\n", encoding="utf-8")

    outcome, _ = _run(
        tmp_path,
        'files edit main.txt \'{"edits": [{"oldText": "a", "newText": "x"}]}\'',
    )

    assert outcome == _ExecutionOutcome.exited()
    assert target.read_text(encoding="utf-8") == "xbc\n"


def test_files_edit_preserves_crlf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "windows.txt"
    target.write_bytes(b"line1\r\nline2\r\n")

    outcome, _ = _run(
        tmp_path,
        "files edit windows.txt <<'EDI'\n"
        '{"edits": [{"oldText": "line2", "newText": "line3"}]}\n'
        "EDI",
    )

    assert outcome == _ExecutionOutcome.exited()
    assert target.read_bytes() == b"line1\r\nline3\r\n"


def test_files_edit_preserves_utf8_bom(tmp_path: Path) -> None:
    target = tmp_path / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfcontent")

    outcome, _ = _run(
        tmp_path,
        "files edit bom.txt <<'EDI'\n"
        '{"edits": [{"oldText": "content", "newText": "changed"}]}\n'
        "EDI",
    )

    assert outcome == _ExecutionOutcome.exited()
    assert target.read_bytes() == b"\xef\xbb\xbfchanged"


@pytest.mark.parametrize(
    ("initial", "payload", "message"),
    (
        (
            "hello",
            '{"edits": [{"oldText": "goodbye", "newText": "x"}]}',
            "Could not find",
        ),
        (
            "abab",
            '{"edits": [{"oldText": "ab", "newText": "x"}]}',
            "Found 2 occurrences",
        ),
        (
            "abcdef",
            '{"edits": [{"oldText": "ab", "newText": "x"}, {"oldText": "bc", "newText": "y"}]}',
            "overlap",
        ),
        ("x", '{"edits": [{"oldText": "x", "newText": "x"}]}', "No changes made"),
    ),
)
def test_files_edit_rejections_leave_file_untouched(
    tmp_path: Path,
    initial: str,
    payload: str,
    message: str,
) -> None:
    target = tmp_path / "main.txt"
    target.write_text(initial, encoding="utf-8")

    outcome, output = _run(
        tmp_path,
        f"files edit main.txt <<'EDI'\n{payload}\nEDI",
    )

    assert outcome.status == "failed"
    assert message in output.text("stderr")
    assert output.text("stdout") == ""
    assert target.read_text(encoding="utf-8") == initial


def test_files_edit_rejects_invalid_utf8_file(tmp_path: Path) -> None:
    target = tmp_path / "binary.bin"
    target.write_bytes(b"\xff\xfe\x00\x80")

    outcome, output = _run(
        tmp_path,
        "files edit binary.bin <<'EDI'\n"
        '{"edits": [{"oldText": "x", "newText": "y"}]}\n'
        "EDI",
    )

    assert outcome.status == "failed"
    assert "not valid UTF-8" in output.text("stderr")
    assert target.read_bytes() == b"\xff\xfe\x00\x80"


def test_files_edit_rejects_missing_file(tmp_path: Path) -> None:
    outcome, output = _run(
        tmp_path,
        "files edit absent.txt <<'EDI'\n"
        '{"edits": [{"oldText": "x", "newText": "y"}]}\n'
        "EDI",
    )

    assert outcome.status == "failed"
    assert "No such file" in output.text("stderr")


@pytest.mark.parametrize(
    "payload",
    (
        "not json",
        '{"edits": []}',
    ),
)
def test_files_edit_invalid_payload_is_a_usage_error(
    tmp_path: Path,
    payload: str,
) -> None:
    outcome, output = _run(
        tmp_path,
        f"files edit main.txt <<'EDI'\n{payload}\nEDI",
    )

    assert outcome.status == "failed"
    assert output.text("stderr") != ""
    assert output.text("stdout") == ""


def test_files_edit_copies_up_in_view_lower_link(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repertoire = tmp_path / "repertoire"
    workspace.mkdir()
    (repertoire / "tools").mkdir(parents=True)
    lower = repertoire / "tools" / "calc.py"
    lower.write_text("VALUE = 1\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    visible = workspace / ".workspace" / "tools" / "calc.py"
    assert visible.is_symlink()

    outcome, _ = _run(
        workspace,
        "files edit .workspace/tools/calc.py <<'EDI'\n"
        '{"edits": [{"oldText": "VALUE = 1", "newText": "VALUE = 2"}]}\n'
        "EDI",
        view=view,
    )

    assert outcome == _ExecutionOutcome.exited()
    assert not visible.is_symlink()
    assert visible.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert lower.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert view.inspect("tools/calc.py").provenance == "workspace"


def _run(
    cwd: Path,
    command: str,
    *,
    view: _CapabilityView | None = None,
) -> tuple[_ExecutionOutcome, _RecordedOutput]:
    output = _RecordedOutput()
    backend = _LocalBackendWorkspace(cwd, {})
    backend.bind_capability_view(view)
    execution = _FileHandler(backend.filesystem).prepare(
        parse_shell_ast(command),
        _CommandContext(workspace=str(cwd), cwd=str(cwd), environment={}),
    )
    outcome = asyncio.run(execution.run(output))
    return outcome, output


class _RecordedOutput:
    def __init__(self) -> None:
        self._chunks: list[tuple[str, bytes]] = []

    async def write(self, stream: str, data: bytes) -> None:
        self._chunks.append((stream, data))

    def text(self, stream: str) -> str:
        return "".join(
            data.decode("utf-8") for name, data in self._chunks if name == stream
        )
