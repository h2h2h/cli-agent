"""Unit tests for the files write Runtime command handler."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

import pytest

from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalCapabilityView,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionOutcome,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.handlers.files import _FileHandler


def test_files_write_creates_file_and_reports_byte_count(tmp_path: Path) -> None:
    outcome, output = _write(tmp_path, "files write hello.txt", stdin="line1\nline2\n")

    target = tmp_path / "hello.txt"
    assert outcome == _ExecutionOutcome.exited()
    assert target.read_text(encoding="utf-8") == "line1\nline2\n"
    assert output.text("stdout") == f"wrote 12 bytes to {target}\n"
    assert output.text("stderr") == ""


def test_files_write_creates_parent_directories(tmp_path: Path) -> None:
    outcome, _ = _write(
        tmp_path,
        "files write nested/deep/file.txt",
        stdin="content\n",
    )

    target = tmp_path / "nested" / "deep" / "file.txt"
    assert outcome == _ExecutionOutcome.exited()
    assert target.read_text(encoding="utf-8") == "content\n"


def test_files_write_overwrites_content_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / "keep.py"
    target.write_text("old\n", encoding="utf-8")
    os.chmod(target, 0o750)

    outcome, output = _write(tmp_path, "files write keep.py", stdin="new\n")

    assert outcome == _ExecutionOutcome.exited()
    assert target.read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    assert output.text("stdout") == f"wrote 4 bytes to {target}\n"


def test_files_write_new_file_uses_default_mode(tmp_path: Path) -> None:
    outcome, _ = _write(tmp_path, "files write fresh.py", stdin="x\n")

    target = tmp_path / "fresh.py"
    assert outcome == _ExecutionOutcome.exited()
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_files_write_empty_stdin_creates_empty_file(tmp_path: Path) -> None:
    outcome, output = _write(tmp_path, "files write empty.txt", stdin="")

    target = tmp_path / "empty.txt"
    assert outcome == _ExecutionOutcome.exited()
    assert target.read_text(encoding="utf-8") == ""
    assert output.text("stdout") == f"wrote 0 bytes to {target}\n"


def test_files_write_preserves_stdin_content_exactly(tmp_path: Path) -> None:
    content = (
        'EOF\nEDI\n$HOME  `ls`  "quoted"\n'
        "std::function's callables\n  keep   spaces  "
    )
    outcome, output = _write(
        tmp_path,
        "files write exact.txt",
        stdin=content,
    )

    target = tmp_path / "exact.txt"
    assert outcome == _ExecutionOutcome.exited()
    assert target.read_text(encoding="utf-8") == content
    assert output.text("stdout") == f"wrote {len(content.encode())} bytes to {target}\n"


def test_files_write_preserves_unicode_and_trailing_newline(tmp_path: Path) -> None:
    content = "héllo 世界\n最后一行\n"
    outcome, output = _write(tmp_path, "files write unicode.txt", stdin=content)

    target = tmp_path / "unicode.txt"
    assert outcome == _ExecutionOutcome.exited()
    assert target.read_bytes() == content.encode("utf-8")
    assert output.text("stdout") == f"wrote {len(content.encode())} bytes to {target}\n"


def test_files_write_without_stdin_fails_clearly(tmp_path: Path) -> None:
    outcome, output = _write(tmp_path, "files write missing.txt")

    assert outcome.status == "failed"
    assert "requires payload in exec.stdin" in output.text("stderr")
    assert output.text("stdout") == ""
    assert not (tmp_path / "missing.txt").exists()


def test_files_write_rejects_directory_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()

    outcome, output = _write(tmp_path, "files write target", stdin="x\n")

    assert outcome.status == "failed"
    assert "failed to write" in output.text("stderr")
    assert output.text("stdout") == ""


def test_files_write_rejects_nul_in_path(tmp_path: Path) -> None:
    outcome, output = _write(tmp_path, "files write bad\x00path", stdin="x\n")

    assert outcome.status == "failed"
    assert "null" in output.text("stderr")


def test_files_write_to_unwritable_directory_fails(tmp_path: Path) -> None:
    if os.name == "posix" and os.geteuid() == 0:
        pytest.skip("permission tests are meaningless as root")
    parent = tmp_path / "locked"
    parent.mkdir()
    os.chmod(parent, 0o500)
    try:
        outcome, output = _write(
            tmp_path,
            "files write locked/data.txt",
            stdin="x\n",
        )

        assert outcome.status == "failed"
        assert "failed to write" in output.text("stderr")
        assert not (parent / "data.txt").exists()
    finally:
        os.chmod(parent, 0o700)


def test_files_write_usage_errors_fail_without_shell(tmp_path: Path) -> None:
    commands = (
        "files write",
        "files write f extra",
        "files write f <<'EOF'\nline\nEOF",
        "files write f > out.txt",
    )
    for command in commands:
        outcome, output = _write(tmp_path, command, stdin="x\n")
        assert outcome.status == "failed"
        assert output.text("stderr") != ""
        assert output.text("stdout") == ""
        assert not (tmp_path / "f").exists()


def test_files_write_copies_up_in_view_lower_link(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    repertoire = tmp_path / "repertoire"
    workspace.mkdir()
    (repertoire / "tools").mkdir(parents=True)
    lower = repertoire / "tools" / "calc.py"
    lower.write_text("LOWER = 1\n", encoding="utf-8")
    view = _LocalCapabilityView.materialize(workspace / ".workspace", repertoire)
    visible = workspace / ".workspace" / "tools" / "calc.py"
    assert visible.is_symlink()

    outcome, _ = _write(
        workspace,
        "files write .workspace/tools/calc.py",
        stdin="NEW = 2\n",
        view=view,
    )

    assert outcome == _ExecutionOutcome.exited()
    assert not visible.is_symlink()
    assert visible.read_text(encoding="utf-8") == "NEW = 2\n"
    assert lower.read_text(encoding="utf-8") == "LOWER = 1\n"
    assert asyncio.run(view.inspect("tools/calc.py")).provenance == "workspace"


def _write(
    cwd: Path,
    command: str,
    *,
    stdin: str | None = None,
    view: _LocalCapabilityView | None = None,
) -> tuple[_ExecutionOutcome, _RecordedOutput]:
    output = _RecordedOutput()
    backend = _LocalBackendWorkspace(cwd, {}, view)
    execution = _FileHandler(backend.filesystem).prepare(
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
