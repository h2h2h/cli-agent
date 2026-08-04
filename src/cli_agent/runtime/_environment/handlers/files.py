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

    def __init__(self, capability_view: _CapabilityView | None = None) -> None:
        self._capability_view = capability_view

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
            )
        return _text_execution("files edit is not implemented yet\n", success=False)


def _write_execution(
    path: str,
    content: str,
    context: _CommandContext,
    capability_view: _CapabilityView | None,
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
        await output.write(
            "stdout",
            f"wrote {len(data)} bytes to {target}\n".encode(),
        )
        return _ExecutionOutcome.exited()

    return _InlineExecution(execute)


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
