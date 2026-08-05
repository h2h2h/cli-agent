"""Reserved Files command grammar, mutation facts, and handler.

The ``files`` command family is a single-module Runtime command: grammar
facts, pure parsing, and the ``_FileHandler`` live together, mirroring
``cd.py``/``export.py``/``tools.py``.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from cli_agent.runtime._capability.command_parser import (
    HereDocRedirect,
    ShellParseResult,
    ShellRedirect,
    ShellWord,
    SimpleCommand,
)
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionOutcome,
    _ExecutionOutput,
    _PreparedExecution,
)
from cli_agent.runtime._environment.handlers.cd import _target_path
from cli_agent.runtime._environment.handlers.executions import (
    _InlineExecution,
    _text_execution,
)

if TYPE_CHECKING:
    from cli_agent.runtime._capability.library.catalog import _LibraryCatalog
    from cli_agent.runtime._capability.view import _CapabilityView

_USAGE = (
    "Usage: files write <path> <<'EOF' ... EOF; "
    "files edit <path> <<'EDI' {...} EDI or '<json>'"
)


@dataclass(frozen=True, slots=True)
class FileEdit:
    """One exact text replacement on a single target file."""

    old_text: str
    new_text: str


@dataclass(frozen=True, slots=True)
class FileCommand:
    """Trusted classification of one reserved top-level ``files`` command."""

    operation: Literal["write", "edit", "invalid"]
    valid: bool
    validation_error: str | None = None
    path: str | None = None
    content: str | None = None
    edits: tuple[FileEdit, ...] = ()


def parse_files_command(command: ShellParseResult) -> FileCommand | None:
    """Parse reserved Files grammar into independent mutation facts.

    Args:
        command (`ShellParseResult`):
            The parsed command to classify.

    Returns:
        ``None`` when the command head is not ``files``; otherwise one
        ``FileCommand`` with either mutation facts or a usage diagnostic.
    """

    if command.command_head != "files":
        return None
    root = command.root
    if not isinstance(root, SimpleCommand) or root.prefix_assignments:
        return _invalid(_USAGE)
    return _files_facts(root)


def _files_facts(command: SimpleCommand) -> FileCommand:
    if not command.argv:
        return _invalid(_USAGE)
    subcommand = command.argv[0]
    if subcommand.value not in {"write", "edit"}:
        return _invalid(f"unknown files subcommand: {subcommand.text}")
    if len(command.argv) < 2:
        return _invalid(f"files {subcommand.value} requires a path")
    path = command.argv[1]
    if path.value is None:
        return _invalid(
            f"files {subcommand.value} path must be statically known: {path.text}"
        )
    if subcommand.value == "write":
        return _write_facts(path.value, command.argv[2:], command.redirects)
    return _edit_facts(path.value, command.argv[2:], command.redirects)


def _write_facts(
    path: str,
    argv_rest: tuple[ShellWord, ...],
    redirects: tuple[ShellRedirect, ...],
) -> FileCommand:
    if argv_rest:
        return _invalid("files write accepts exactly one path")
    heredoc = _single_heredoc(redirects)
    if heredoc is None:
        return _invalid("files write requires exactly one <<'EOF' heredoc payload")
    if heredoc.operator != "<<" or heredoc.delimiter.value != "EOF":
        return _invalid(
            f"files write heredoc must use an exact <<'EOF' delimiter "
            f"(got {heredoc.operator}{heredoc.delimiter.text})"
        )
    return FileCommand(
        operation="write",
        valid=True,
        path=path,
        content=heredoc.body.text,
    )


def _edit_facts(
    path: str,
    argv_rest: tuple[ShellWord, ...],
    redirects: tuple[ShellRedirect, ...],
) -> FileCommand:
    if len(argv_rest) > 1:
        return _invalid("files edit accepts at most one quoted JSON payload")
    if argv_rest:
        if redirects:
            return _invalid(
                "files edit accepts either a quoted payload or a heredoc, not both"
            )
        payload = argv_rest[0]
        if payload.quote is None:
            return _invalid(f"files edit payload must be quoted JSON: {payload.text}")
        return _edit_payload_facts(path, payload.quoted_content or "")
    heredoc = _single_heredoc(redirects)
    if heredoc is None:
        return _invalid(
            "files edit requires one <<'EDI' heredoc or a quoted '<json>' payload"
        )
    if heredoc.operator != "<<" or heredoc.delimiter.value != "EDI":
        return _invalid(
            f"files edit heredoc must use an exact <<'EDI' delimiter "
            f"(got {heredoc.operator}{heredoc.delimiter.text})"
        )
    return _edit_payload_facts(path, heredoc.body.text)


def _edit_payload_facts(path: str, payload: str) -> FileCommand:
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        return _invalid(f"files edit payload is not valid JSON: {exc.msg}")
    if not isinstance(document, dict) or not isinstance(document.get("edits"), list):
        return _invalid("files edit payload must be a JSON object with an edits array")
    if not document["edits"]:
        return _invalid("files edit edits array must not be empty")
    parsed: list[FileEdit] = []
    for index, item in enumerate(document["edits"], start=1):
        if not isinstance(item, dict):
            return _invalid(f"files edit edits[{index}] must be a JSON object")
        old_text = item.get("oldText")
        new_text = item.get("newText")
        if not isinstance(old_text, str) or not old_text:
            return _invalid(f"files edit edits[{index}] requires a non-empty oldText")
        if not isinstance(new_text, str) or not new_text:
            return _invalid(f"files edit edits[{index}] requires a non-empty newText")
        parsed.append(FileEdit(old_text=old_text, new_text=new_text))
    return FileCommand(operation="edit", valid=True, path=path, edits=tuple(parsed))


def _single_heredoc(
    redirects: tuple[ShellRedirect, ...],
) -> HereDocRedirect | None:
    """Return the single heredoc redirect, or None for any other shape."""

    if len(redirects) != 1:
        return None
    redirect = redirects[0]
    if not isinstance(redirect, HereDocRedirect):
        return None
    return redirect


def _invalid(reason: str) -> FileCommand:
    """Return one stable usage diagnostic for an unsupported Files shape."""

    return FileCommand(operation="invalid", valid=False, validation_error=reason)


class _FileHandler:
    """Prepare files write and edit operations from trusted Files facts."""

    def __init__(
        self,
        capability_view: _CapabilityView | None = None,
        library_catalog: _LibraryCatalog | None = None,
    ) -> None:
        self._capability_view = capability_view
        self._library_catalog = library_catalog

    def prepare(
        self,
        command: ShellParseResult,
        context: _CommandContext,
    ) -> _PreparedExecution:
        facts = parse_files_command(command)
        if facts is None:
            raise RuntimeError("File handler received an ordinary command")
        if not facts.valid:
            return _text_execution(
                (facts.validation_error or "Invalid files command") + "\n",
                success=False,
            )
        if facts.operation == "write" and facts.path is not None:
            return _write_execution(
                facts.path,
                facts.content or "",
                context,
                self._capability_view,
                self._library_catalog,
            )
        if facts.operation == "edit" and facts.path is not None:
            return _edit_execution(
                facts.path,
                facts.edits,
                context,
                self._capability_view,
                self._library_catalog,
            )
        return _text_execution("Invalid files command\n", success=False)


def _write_execution(
    path: str,
    content: str,
    context: _CommandContext,
    capability_view: _CapabilityView | None,
    library_catalog: _LibraryCatalog | None,
) -> _InlineExecution:
    target = Path(os.path.normpath(str(_target_path(path, context.cwd))))

    async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
        if capability_view is not None:
            try:
                capability_view.prepare_path(target)
            except ValueError as exc:
                await output.write("stderr", f"{exc}\n".encode())
                return _ExecutionOutcome.failed(1)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8")
            _atomic_write(target, data)
        except (OSError, ValueError) as exc:
            await output.write(
                "stderr",
                f"failed to write {target}: {exc}\n".encode(),
            )
            return _ExecutionOutcome.failed(1)
        if library_catalog is not None:
            library_catalog.mark_path_dirty(target)
        await output.write(
            "stdout",
            f"wrote {len(data)} bytes to {target}\n".encode(),
        )
        return _ExecutionOutcome.exited()

    return _InlineExecution(execute)


def _edit_execution(
    path: str,
    edits: tuple[FileEdit, ...],
    context: _CommandContext,
    capability_view: _CapabilityView | None,
    library_catalog: _LibraryCatalog | None,
) -> _InlineExecution:
    target = Path(os.path.normpath(str(_target_path(path, context.cwd))))

    async def execute(output: _ExecutionOutput) -> _ExecutionOutcome:
        if capability_view is not None:
            try:
                capability_view.prepare_path(target)
            except ValueError as exc:
                await output.write("stderr", f"{exc}\n".encode())
                return _ExecutionOutcome.failed(1)
        try:
            raw = target.read_bytes()
        except OSError as exc:
            await output.write(
                "stderr",
                f"failed to edit {target}: {exc}\n".encode(),
            )
            return _ExecutionOutcome.failed(1)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            await output.write(
                "stderr",
                f"failed to edit {target}: file is not valid UTF-8\n".encode(),
            )
            return _ExecutionOutcome.failed(1)
        bom, content = _split_bom(content)
        line_ending = _detect_line_ending(content)
        try:
            updated = apply_edits(content.replace("\r\n", "\n"), edits, str(target))
            if line_ending == "\r\n":
                updated = updated.replace("\n", "\r\n")
            _atomic_write(target, (bom + updated).encode("utf-8"))
        except ValueError as exc:
            await output.write("stderr", f"{exc}\n".encode())
            return _ExecutionOutcome.failed(1)
        except OSError as exc:
            await output.write(
                "stderr",
                f"failed to edit {target}: {exc}\n".encode(),
            )
            return _ExecutionOutcome.failed(1)
        if library_catalog is not None:
            library_catalog.mark_path_dirty(target)
        await output.write(
            "stdout",
            f"replaced {len(edits)} block(s) in {target}\n".encode(),
        )
        return _ExecutionOutcome.exited()

    return _InlineExecution(execute)


def _split_bom(content: str) -> tuple[str, str]:
    """Return the leading BOM (if any) and the content without it."""

    if content.startswith("\ufeff"):
        return "\ufeff", content[1:]
    return "", content


def _detect_line_ending(content: str) -> str:
    """Return ``\\r\\n`` when the first newline is CRLF, else ``\\n``."""

    first_newline = content.find("\n")
    if first_newline > 0 and content[first_newline - 1] == "\r":
        return "\r\n"
    return "\n"


def _atomic_write(path: Path, data: bytes) -> None:
    """Atomically replace one file, preserving its mode when present."""

    try:
        mode = stat.S_IMODE(path.stat().st_mode) if os.path.lexists(path) else None
    except OSError:
        mode = None
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".cli-agent-write-",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            os.fchmod(stream.fileno(), 0o644 if mode is None else mode)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            with suppress(OSError):
                temporary.unlink()


def apply_edits(content: str, edits: tuple[FileEdit, ...], path: str) -> str:
    """Apply exact-text replacements on LF-normalized content.

    Every oldText is matched against the original content and must occur
    exactly once; replacements are then applied in reverse position order
    so offsets stay stable.

    Args:
        content (`str`):
            The original content normalized to LF line endings.
        edits (`tuple[FileEdit, ...]`):
            Exact replacements to apply, matched against ``content``.
        path (`str`):
            The display path used in rejection messages.

    Returns:
        The fully applied content.

    Raises:
        ValueError: With an actionable message when an edit is empty, not
            found, duplicated, overlapping, or produces no change.
    """

    normalized = tuple(
        FileEdit(
            old_text=edit.old_text.replace("\r\n", "\n"),
            new_text=edit.new_text.replace("\r\n", "\n"),
        )
        for edit in edits
    )
    total = len(normalized)
    for index, edit in enumerate(normalized):
        if not edit.old_text:
            raise ValueError(_empty_old_text_error(path, index, total))

    matches: list[tuple[int, int, int]] = []
    for index, edit in enumerate(normalized):
        start = content.find(edit.old_text)
        if start < 0:
            raise ValueError(_not_found_error(path, index, total))
        occurrences = content.count(edit.old_text)
        if occurrences > 1:
            raise ValueError(_duplicate_error(path, index, total, occurrences))
        matches.append((index, start, start + len(edit.old_text)))

    matches.sort(key=lambda match: match[1])
    for (previous, _, previous_end), (index, start, _) in zip(
        matches,
        matches[1:],
        strict=False,
    ):
        if previous_end > start:
            raise ValueError(_overlap_error(path, previous, index))

    updated = content
    for index, start, end in reversed(matches):
        updated = updated[:start] + normalized[index].new_text + updated[end:]
    if updated == content:
        raise ValueError(_no_change_error(path, total))
    return updated


def _empty_old_text_error(path: str, index: int, total: int) -> str:
    if total == 1:
        return f"oldText must not be empty in {path}."
    return f"edits[{index}].oldText must not be empty in {path}."


def _not_found_error(path: str, index: int, total: int) -> str:
    if total == 1:
        return (
            f"Could not find the exact text in {path}. The old text must match "
            "exactly including all whitespace and newlines."
        )
    return (
        f"Could not find edits[{index}] in {path}. The oldText must match exactly "
        "including all whitespace and newlines."
    )


def _duplicate_error(
    path: str,
    index: int,
    total: int,
    occurrences: int,
) -> str:
    if total == 1:
        return (
            f"Found {occurrences} occurrences of the text in {path}. The text "
            "must be unique. Please provide more context to make it unique."
        )
    return (
        f"Found {occurrences} occurrences of edits[{index}] in {path}. Each "
        "oldText must be unique. Please provide more context to make it unique."
    )


def _overlap_error(path: str, previous: int, current: int) -> str:
    return (
        f"edits[{previous}] and edits[{current}] overlap in {path}. Merge them "
        "into one edit or target disjoint regions."
    )


def _no_change_error(path: str, total: int) -> str:
    if total == 1:
        return (
            f"No changes made to {path}. The replacement produced identical "
            "content. This might indicate an issue with special characters or "
            "the text not existing as expected."
        )
    return f"No changes made to {path}. The replacements produced identical content."
