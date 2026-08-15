"""Project instruction loader Host source tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.runtime._project_instructions import (
    MAX_PROJECT_INSTRUCTION_BYTES,
    _load_project_instructions,
    _ProjectInstructions,
)

_AGENTS = "AGENTS.md"


def test_missing_file_returns_none(tmp_path: Path) -> None:
    assert _load_project_instructions(tmp_path) is None


def test_empty_file_returns_none(tmp_path: Path) -> None:
    (tmp_path / _AGENTS).write_bytes(b"")

    assert _load_project_instructions(tmp_path) is None


def test_whitespace_only_file_returns_none(tmp_path: Path) -> None:
    for content in (b"   \n\t", "\u00a0\u2002\n".encode("utf-8")):
        (tmp_path / _AGENTS).write_bytes(content)

        assert _load_project_instructions(tmp_path) is None


def test_regular_utf8_content_returns_snapshot(tmp_path: Path) -> None:
    content = "# Project rules\n\nrun `uv run pytest`.\n"
    (tmp_path / _AGENTS).write_text(content, encoding="utf-8")

    result = _load_project_instructions(tmp_path)

    assert result == _ProjectInstructions(
        source=str(tmp_path / _AGENTS),
        text=content,
    )


def test_crlf_and_multibyte_content_are_preserved(tmp_path: Path) -> None:
    content = "# 构建\r\n使用 `uv run pytest`。\n"
    (tmp_path / _AGENTS).write_text(content, encoding="utf-8")

    result = _load_project_instructions(tmp_path)

    assert result is not None
    assert result.text == content


def test_directory_fails(tmp_path: Path) -> None:
    (tmp_path / _AGENTS).mkdir()

    with pytest.raises(ValueError, match="expected a regular file"):
        _load_project_instructions(tmp_path)


def test_stat_over_limit_fails(tmp_path: Path) -> None:
    (tmp_path / _AGENTS).write_bytes(b"x" * (MAX_PROJECT_INSTRUCTION_BYTES + 1))

    with pytest.raises(ValueError, match="32768-byte limit"):
        _load_project_instructions(tmp_path)


def test_maximum_size_succeeds(tmp_path: Path) -> None:
    content = b"x" * MAX_PROJECT_INSTRUCTION_BYTES
    (tmp_path / _AGENTS).write_bytes(content)

    result = _load_project_instructions(tmp_path)

    assert result is not None
    assert result.text == content.decode("utf-8")


def test_invalid_utf8_fails_with_source(tmp_path: Path) -> None:
    (tmp_path / _AGENTS).write_bytes(b"# rules\n\xff\xfe broken")

    with pytest.raises(ValueError, match=str(tmp_path)) as excinfo:
        _load_project_instructions(tmp_path)

    assert "decode" in str(excinfo.value)
    assert "not valid UTF-8" in str(excinfo.value)


def test_symlink_to_regular_file_loads(tmp_path: Path) -> None:
    target = tmp_path / "rules.md"
    target.write_text("# linked rules\n", encoding="utf-8")
    (tmp_path / _AGENTS).symlink_to(target.name)

    result = _load_project_instructions(tmp_path)

    assert result is not None
    assert result.text == "# linked rules\n"


def test_broken_symlink_returns_none(tmp_path: Path) -> None:
    (tmp_path / _AGENTS).symlink_to("missing-target.md")

    assert _load_project_instructions(tmp_path) is None
