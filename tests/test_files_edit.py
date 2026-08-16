"""Unit tests for the files edit Runtime command handler."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cli_agent._adapters.local.view import _LocalCapabilityView
from cli_agent.runtime._backend import _FileEdit
from cli_agent.runtime._backend.edit import apply_edits
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.sources import _FileSource
from cli_agent.runtime._execution import (
    ExitStatus,
)


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
        "files edit main.txt",
        payload={"edits": [{"oldText": "world", "newText": "there"}]},
    )

    assert outcome == ExitStatus(0)
    assert target.read_text(encoding="utf-8") == "hello there\n"
    assert output.text("stdout") == f"replaced 1 block(s) in {target}\n"
    assert output.text("stderr") == ""


def test_files_edit_replaces_multiple_blocks_in_one_call(tmp_path: Path) -> None:
    target = tmp_path / "main.txt"
    target.write_text("alpha beta gamma\n", encoding="utf-8")

    outcome, output = _run(
        tmp_path,
        "files edit main.txt",
        payload={
            "edits": [
                {"oldText": "alpha", "newText": "1"},
                {"oldText": "gamma", "newText": "3"},
            ]
        },
    )

    assert outcome == ExitStatus(0)
    assert target.read_text(encoding="utf-8") == "1 beta 3\n"
    assert output.text("stdout") == f"replaced 2 block(s) in {target}\n"


def test_files_edit_empty_new_text_deletes_matched_block(tmp_path: Path) -> None:
    target = tmp_path / "main.txt"
    target.write_text("keep\nremove me\nkeep\n", encoding="utf-8")

    outcome, output = _run(
        tmp_path,
        "files edit main.txt",
        payload={"edits": [{"oldText": "remove me\n", "newText": ""}]},
    )

    assert outcome == ExitStatus(0)
    assert target.read_text(encoding="utf-8") == "keep\nkeep\n"
    assert output.text("stdout") == f"replaced 1 block(s) in {target}\n"


def test_files_edit_preserves_crlf_line_endings(tmp_path: Path) -> None:
    target = tmp_path / "windows.txt"
    target.write_bytes(b"line1\r\nline2\r\n")

    outcome, _ = _run(
        tmp_path,
        "files edit windows.txt",
        payload={"edits": [{"oldText": "line2", "newText": "line3"}]},
    )

    assert outcome == ExitStatus(0)
    assert target.read_bytes() == b"line1\r\nline3\r\n"


def test_files_edit_preserves_utf8_bom(tmp_path: Path) -> None:
    target = tmp_path / "bom.txt"
    target.write_bytes(b"\xef\xbb\xbfcontent")

    outcome, _ = _run(
        tmp_path,
        "files edit bom.txt",
        payload={"edits": [{"oldText": "content", "newText": "changed"}]},
    )

    assert outcome == ExitStatus(0)
    assert target.read_bytes() == b"\xef\xbb\xbfchanged"


@pytest.mark.parametrize(
    ("initial", "payload", "message"),
    (
        (
            "hello",
            {"edits": [{"oldText": "goodbye", "newText": "x"}]},
            "Could not find",
        ),
        (
            "abab",
            {"edits": [{"oldText": "ab", "newText": "x"}]},
            "Found 2 occurrences",
        ),
        (
            "abcdef",
            {
                "edits": [
                    {"oldText": "ab", "newText": "x"},
                    {"oldText": "bc", "newText": "y"},
                ]
            },
            "overlap",
        ),
        ("x", {"edits": [{"oldText": "x", "newText": "x"}]}, "No changes made"),
    ),
)
def test_files_edit_rejections_leave_file_untouched(
    tmp_path: Path,
    initial: str,
    payload: dict[str, object],
    message: str,
) -> None:
    target = tmp_path / "main.txt"
    target.write_text(initial, encoding="utf-8")

    outcome, output = _run(
        tmp_path,
        "files edit main.txt",
        payload=payload,
    )

    assert outcome == 1
    assert message in output.text("stderr")
    assert output.text("stdout") == ""
    assert target.read_text(encoding="utf-8") == initial


def test_files_edit_rejects_invalid_utf8_file(tmp_path: Path) -> None:
    target = tmp_path / "binary.bin"
    target.write_bytes(b"\xff\xfe\x00\x80")

    outcome, output = _run(
        tmp_path,
        "files edit binary.bin",
        payload={"edits": [{"oldText": "x", "newText": "y"}]},
    )

    assert outcome == 1
    assert "not valid UTF-8" in output.text("stderr")
    assert target.read_bytes() == b"\xff\xfe\x00\x80"


def test_files_edit_rejects_missing_file(tmp_path: Path) -> None:
    outcome, output = _run(
        tmp_path,
        "files edit absent.txt",
        payload={"edits": [{"oldText": "x", "newText": "y"}]},
    )

    assert outcome == 1
    assert "No such file" in output.text("stderr")


def test_files_edit_without_stdin_fails_clearly(tmp_path: Path) -> None:
    outcome, output = _run(tmp_path, "files edit absent.txt")

    assert outcome == 1
    assert "requires payload in exec.stdin" in output.text("stderr")
    assert output.text("stdout") == ""


@pytest.mark.parametrize(
    "stdin",
    (
        "not json",
        "[]",
        '{"edits": "empty"}',
        '{"edits": []}',
        '{"edits": [{}]}',
        '{"edits": [{"oldText": "", "newText": "b"}]}',
        '{"edits": [{"oldText": "a"}]}',
        '{"edits": [{"oldText": "a", "newText": 7}]}',
        '{"edits": ["x"]}',
        '{"edits": [{"oldText": "a", "newText": "b"}, {}]}',
    ),
)
def test_files_edit_invalid_stdin_payload_is_a_usage_error(
    tmp_path: Path,
    stdin: str,
) -> None:
    outcome, output = _run(
        tmp_path,
        "files edit main.txt",
        stdin=stdin,
    )

    assert outcome == 1
    assert output.text("stderr") != ""
    assert output.text("stdout") == ""


def test_files_edit_copies_up_in_view_lower_link(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repertoire = tmp_path / "repertoire"
    workspace.mkdir()
    (repertoire / "tools").mkdir(parents=True)
    lower = repertoire / "tools" / "calc.py"
    lower.write_text("VALUE = 1\n", encoding="utf-8")
    view = _LocalCapabilityView.materialize(workspace / ".workspace", repertoire)
    visible = workspace / ".workspace" / "tools" / "calc.py"
    assert visible.is_symlink()

    outcome, _ = _run(
        workspace,
        "files edit .workspace/tools/calc.py",
        payload={"edits": [{"oldText": "VALUE = 1", "newText": "VALUE = 2"}]},
        view=view,
    )

    assert outcome == ExitStatus(0)
    assert not visible.is_symlink()
    assert visible.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert lower.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert asyncio.run(view.inspect("tools/calc.py")).provenance == "workspace"


def _run(
    cwd: Path,
    command: str,
    *,
    payload: dict[str, object] | None = None,
    stdin: str | None = None,
    view: _LocalCapabilityView | None = None,
) -> tuple[ExitStatus, _RecordedOutput]:
    if payload is not None:
        stdin = json.dumps(payload)
    output = _RecordedOutput()
    backend = _LocalBackendWorkspace(cwd, {})
    execution = _FileSource(backend.filesystem).prepare(
        _ExecutionRequest(command=parse_shell_ast(command), stdin=stdin),
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
