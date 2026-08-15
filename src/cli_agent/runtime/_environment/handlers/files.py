"""Reserved Files command grammar and mutation operations.

Grammar facts and pure parsing live here together with the Workspace
Filesystem mutation operations used by ``_FileSource``. Payloads never
enter the Shell parser: ``files write`` and ``files edit`` read their
content from the ``exec`` ``stdin`` argument.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from cli_agent.runtime._backend import (
    _FileEdit,
    _FileEditRequest,
    _FilesystemError,
    _FileWriteRequest,
    _WorkspaceFilesystem,
)
from cli_agent.runtime._backend.execution import _FilesystemOperation
from cli_agent.runtime._capability.command_parser import (
    HereDocRedirect,
    ShellParseResult,
    ShellRedirect,
    SimpleCommand,
)
from cli_agent.runtime._execution import (
    ExecutionOutputSink,
    ExitStatus,
)

_MarkDirty = Callable[[str], None]

_USAGE = (
    "Usage: files write <path>; files edit <path>; "
    "content or edits JSON is provided in exec stdin"
)


@dataclass(frozen=True, slots=True)
class FileCommand:
    """Trusted classification of one reserved top-level ``files`` command."""

    operation: Literal["write", "edit", "invalid"]
    valid: bool
    validation_error: str | None = None
    path: str | None = None


def parse_files_command(command: ShellParseResult) -> FileCommand | None:
    """Parse reserved Files grammar into independent mutation facts.

    Args:
        command (`ShellParseResult`):
            The parsed command to classify.

    Returns:
        ``None`` when the command head is not ``files``; otherwise one
        ``FileCommand`` with either mutation facts or a usage diagnostic.
        The payload is deliberately absent from the facts: it arrives
        through the ``exec`` ``stdin`` argument.
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
    operation = subcommand.value
    if len(command.argv) != 2:
        if len(command.argv) > 2 and command.argv[2].quote is not None:
            return _invalid(
                f"files {operation} payload must be provided in exec stdin; "
                "quoted JSON arguments are no longer supported"
            )
        return _invalid(f"files {operation} accepts exactly one path")
    path = command.argv[1]
    if path.value is None:
        return _invalid(f"files {operation} path must be statically known: {path.text}")
    redirect_error = _redirect_error(operation, command.redirects)
    if redirect_error is not None:
        return _invalid(redirect_error)
    return FileCommand(operation=operation, valid=True, path=path.value)


def _redirect_error(operation: str, redirects: tuple[ShellRedirect, ...]) -> str | None:
    """Return the stable payload-source diagnostic for attached redirects."""

    if not redirects:
        return None
    if any(isinstance(redirect, HereDocRedirect) for redirect in redirects):
        return (
            f"files {operation} payload must be provided in exec stdin; "
            "heredocs are no longer supported"
        )
    return f"files {operation} does not accept redirects"


def parse_edit_payload(payload: str) -> tuple[_FileEdit, ...]:
    """Parse one ``files edit`` payload, raising ``ValueError`` on failure.

    Every rejection carries a stable message a model can correct in the
    next call. ``newText`` must exist and be a string but may be empty to
    delete the matched text.
    """

    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"files edit stdin is not valid JSON: {exc.msg}") from None
    if not isinstance(document, dict) or not isinstance(document.get("edits"), list):
        raise ValueError("files edit stdin must be a JSON object with an edits array")
    if not document["edits"]:
        raise ValueError("files edit edits array must not be empty")
    parsed: list[_FileEdit] = []
    for index, item in enumerate(document["edits"], start=1):
        if not isinstance(item, dict):
            raise ValueError(f"files edit edits[{index}] must be a JSON object")
        old_text = item.get("oldText")
        new_text = item.get("newText")
        if not isinstance(old_text, str) or not old_text:
            raise ValueError(f"files edit edits[{index}] requires a non-empty oldText")
        if not isinstance(new_text, str):
            raise ValueError(f"files edit edits[{index}] requires a string newText")
        parsed.append(_FileEdit(old_text=old_text, new_text=new_text))
    return tuple(parsed)


def _invalid(reason: str) -> FileCommand:
    """Return one stable usage diagnostic for an unsupported Files shape."""

    return FileCommand(operation="invalid", valid=False, validation_error=reason)


def _write_operation(
    filesystem: _WorkspaceFilesystem,
    path: str,
    content: str,
    cwd: str,
    mark_dirty: _MarkDirty | None,
) -> _FilesystemOperation:
    data = content.encode("utf-8")

    async def execute(output: ExecutionOutputSink) -> ExitStatus:
        target = path
        try:
            target = filesystem.resolve(path, cwd).path
            result = await filesystem.write(
                _FileWriteRequest(path=target, content=data)
            )
        except _FilesystemError as exc:
            await output.write(
                "stderr",
                f"failed to write {target}: {exc}\n".encode(),
            )
            return ExitStatus(1)
        if mark_dirty is not None:
            mark_dirty(target)
        await output.write(
            "stdout",
            f"wrote {result.bytes_written} bytes to {target}\n".encode(),
        )
        return ExitStatus(0)

    return execute


def _edit_operation(
    filesystem: _WorkspaceFilesystem,
    path: str,
    edits: tuple[_FileEdit, ...],
    cwd: str,
    mark_dirty: _MarkDirty | None,
) -> _FilesystemOperation:
    async def execute(output: ExecutionOutputSink) -> ExitStatus:
        target = path
        try:
            target = filesystem.resolve(path, cwd).path
            result = await filesystem.edit(_FileEditRequest(path=target, edits=edits))
        except _FilesystemError as exc:
            if exc.kind == "edit_failed":
                await output.write("stderr", f"{exc}\n".encode())
            else:
                await output.write(
                    "stderr",
                    f"failed to edit {target}: {exc}\n".encode(),
                )
            return ExitStatus(1)
        if mark_dirty is not None:
            mark_dirty(target)
        await output.write(
            "stdout",
            f"replaced {result.blocks_replaced} block(s) in {target}\n".encode(),
        )
        return ExitStatus(0)

    return execute
