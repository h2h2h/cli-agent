"""Execution Source contracts and the four built-in source families.

An ``ExecutionSource`` produces ``ExecutionHandle`` objects for one command
family. ``prepare`` is synchronous and free of external side effects:
subprocess, container, worker, and filesystem resources are created only
when the returned handle runs.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Protocol, runtime_checkable

from cli_agent.runtime._backend import (
    _FilesystemExecution,
    _ShellExecutionRequest,
    _ToolBinding,
    _ToolExecutionRequest,
    _WorkspaceFilesystem,
)
from cli_agent.runtime._capability.command_parser import ShellParseResult
from cli_agent.runtime._capability.deployment import ToolExecutor
from cli_agent.runtime._capability.overlay import CapabilityOverlay
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.grammar import parse_tool_command
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionRequest,
)
from cli_agent.runtime._environment.handlers.cd import _prepare_cd
from cli_agent.runtime._environment.handlers.executions import _text_execution
from cli_agent.runtime._environment.handlers.export import _prepare_export
from cli_agent.runtime._environment.handlers.files import (
    _edit_operation,
    _write_operation,
    parse_edit_payload,
    parse_files_command,
)
from cli_agent.runtime._execution import ExecutionHandle
from cli_agent.runtime._workspace import Workspace

_ParallelSafety = bool | Callable[[ShellParseResult], bool]
_Preparer = Callable[[_ExecutionRequest, _CommandContext], ExecutionHandle]


@runtime_checkable
class ExecutionSource(Protocol):
    """Produce ExecutionHandles for one command family.

    Sources never see exec ids, queues, cursors, truncation, or model
    ``ToolResult`` payloads; they only translate one parsed request and
    Session context into one handle.
    """

    isolated: bool

    def parallel_safe(self, command: ShellParseResult) -> bool:
        """Return the scheduling fact for one parsed command."""
        ...

    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
        """Prepare one execution without starting work or resources."""
        ...


def _stdin_guard(
    name: str,
    request: _ExecutionRequest,
) -> ExecutionHandle | None:
    """Reject bound stdin for command families that do not consume it."""

    if request.stdin is None:
        return None
    return _text_execution(
        f"`{name}` does not consume exec stdin; omit stdin or use a shell command\n",
        success=False,
    )


class _InlineSource:
    """cd, export, and registered Runtime-local command families."""

    def __init__(
        self,
        name: str,
        prepare: _Preparer,
        *,
        isolated: bool,
        parallel_safe: _ParallelSafety = False,
        consumes_stdin: bool = False,
    ) -> None:
        _validate_head(name)
        if not isinstance(isolated, bool):
            raise TypeError("inline source isolated must be a bool")
        if not isinstance(parallel_safe, bool) and not callable(parallel_safe):
            raise TypeError("inline source parallel_safe must be a bool or callable")
        if not isinstance(consumes_stdin, bool):
            raise TypeError("inline source consumes_stdin must be a bool")
        self.name = name
        self._prepare = prepare
        self.isolated = isolated
        self._parallel_safe = parallel_safe
        self._consumes_stdin = consumes_stdin

    def parallel_safe(self, command: ShellParseResult) -> bool:
        """Evaluate this source's fixed or command-specific schedule fact."""

        value = self._parallel_safe
        return value(command) if callable(value) else value

    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
        guard = _stdin_guard(self.name, request)
        if guard is not None:
            return guard
        return self._prepare(request, context)


class _FileSource:
    """The reserved files command family over the Workspace Filesystem."""

    name = "files"
    isolated = True

    def __init__(
        self,
        filesystem: _WorkspaceFilesystem | None = None,
        mark_dirty: Callable[[str], None] | None = None,
        overlay: CapabilityOverlay | None = None,
    ) -> None:
        self._filesystem = filesystem
        self._mark_dirty = mark_dirty
        self._overlay = overlay

    def parallel_safe(self, command: ShellParseResult) -> bool:
        del command
        return False

    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
        facts = parse_files_command(request.command)
        if facts is None:
            raise RuntimeError("File source received an ordinary command")
        if not facts.valid:
            return _text_execution(
                (facts.validation_error or "Invalid files command") + "\n",
                success=False,
            )
        filesystem = self._filesystem
        if filesystem is None:
            return _text_execution(
                "Workspace filesystem is unavailable\n",
                success=False,
            )
        if facts.operation not in {"write", "edit"} or facts.path is None:
            return _text_execution("Invalid files command\n", success=False)
        stdin = request.stdin
        if stdin is None:
            return _text_execution(
                f"`files {facts.operation}` requires payload in exec.stdin\n",
                success=False,
            )
        if facts.operation == "write":
            execution = _FilesystemExecution(
                _write_operation(
                    filesystem,
                    facts.path,
                    stdin,
                    context.cwd,
                    self._mark_dirty,
                )
            )
            return self._wrap(facts.path, context, execution)
        try:
            edits = parse_edit_payload(stdin)
        except ValueError as exc:
            return _text_execution(f"{exc}\n", success=False)
        execution = _FilesystemExecution(
            _edit_operation(
                filesystem,
                facts.path,
                edits,
                context.cwd,
                self._mark_dirty,
            )
        )
        return self._wrap(facts.path, context, execution)

    def _wrap(
        self,
        path: str,
        context: _CommandContext,
        execution: ExecutionHandle,
    ) -> ExecutionHandle:
        overlay = self._overlay
        filesystem = self._filesystem
        if overlay is None or filesystem is None:
            return execution
        resolved = filesystem.resolve(path, context.cwd)
        return overlay.wrap_file(resolved.path, execution)


class _ShellSource:
    """The ordinary command fallback routed through the Workspace."""

    name = None
    isolated = True

    def __init__(
        self,
        workspace: Workspace,
        *,
        overlay: CapabilityOverlay | None = None,
        parallel_commands: frozenset[str] = frozenset(),
    ) -> None:
        invalid = sorted(
            name
            for name in parallel_commands
            if not name or name.strip() != name or "/" in name or "\\" in name
        )
        if invalid:
            raise ValueError(
                "parallel Shell command names must be non-empty executable basenames"
            )
        self._workspace = workspace
        self._overlay = overlay
        self._parallel_commands = parallel_commands

    def parallel_safe(self, command: ShellParseResult) -> bool:
        """Return whether the parsed Shell command is trusted for parallel use."""

        return bool(
            command.syntax_valid
            and command.executable_basename in self._parallel_commands
            and not command.contains_shell_composition
        )

    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
        stdin = request.stdin
        execution = self._workspace.prepare_shell(
            _ShellExecutionRequest(
                command=request.command,
                cwd=str(context.cwd),
                environment=context.environment,
                input_data=stdin.encode("utf-8") if stdin is not None else None,
            )
        )
        overlay = self._overlay
        if overlay is None:
            return execution
        return overlay.wrap_shell(
            request.command,
            str(context.cwd),
            execution,
        )


class _ToolSource:
    """The reserved tools command family routed through a ToolExecutor."""

    name = "tools"
    isolated = True

    def __init__(
        self,
        catalog: _ToolCatalog | None,
        executor: ToolExecutor | None,
    ) -> None:
        self._catalog = catalog
        self._executor = executor

    def parallel_safe(self, command: ShellParseResult) -> bool:
        """Return the scheduling fact for one parsed tools command."""

        catalog = self._catalog
        if catalog is None:
            return False
        facts = parse_tool_command(command, catalog)
        if facts is None:
            return False
        if facts.operation in {"list", "inspect"}:
            return True
        return bool(
            facts.operation == "run"
            and facts.valid
            and facts.references
            and not facts.has_dynamic_references
            and all(
                reference.valid and reference.parallel_safe
                for reference in facts.references
            )
        )

    def prepare(
        self,
        request: _ExecutionRequest,
        context: _CommandContext,
    ) -> ExecutionHandle:
        guard = _stdin_guard("tools", request)
        if guard is not None:
            return guard
        catalog = self._catalog
        if catalog is None:
            return _text_execution(
                "Tool catalog is unavailable\n",
                success=False,
            )
        facts = parse_tool_command(request.command, catalog)
        if facts is None:
            raise RuntimeError("Tool source received an ordinary command")
        if facts.operation == "list":
            return _text_execution(catalog.render_index(), success=True)
        if facts.operation == "inspect" and facts.name is not None:
            text, success = catalog.render_info(facts.name)
            return _text_execution(text, success=success)
        if facts.operation != "run" or facts.code is None:
            return _text_execution(
                (facts.validation_error or "Invalid tools command") + "\n",
                success=False,
            )
        if not facts.valid:
            return _text_execution(
                (facts.validation_error or "Invalid Tool invocation") + "\n",
                success=False,
            )
        executor = self._executor
        if executor is None:
            return _text_execution(
                "Tool environment is unavailable\n",
                success=False,
            )
        return executor.prepare(
            _ToolExecutionRequest(
                code=facts.code,
                bindings=tuple(
                    _ToolBinding(name=entry.name, path=entry.path)
                    for entry in catalog.valid_entries
                ),
            ),
            context,
        )


def _validate_head(name: str) -> None:
    """Reject malformed command heads without raising protocol errors."""

    if (
        not name
        or name.strip() != name
        or any(character.isspace() for character in name)
    ):
        raise ValueError("command head must be one non-empty token")


class _SourceRegistry:
    """Resolve exact command heads to ExecutionSources before Shell fallback."""

    def __init__(self, entries: Iterable[tuple[str, ExecutionSource]] = ()) -> None:
        self._sources: dict[str, ExecutionSource] = {}
        for name, source in entries:
            self.register(name, source)

    def register(self, name: str, source: ExecutionSource) -> None:
        """Register one source without allowing silent replacement."""

        _validate_head(name)
        if name in self._sources:
            raise ValueError(f"command source already registered: {name}")
        self._sources[name] = source

    def resolve(self, command: ShellParseResult) -> ExecutionSource | None:
        """Return the source selected by the command-head rule."""

        head = command.command_head
        if head is None:
            return None
        return self._sources.get(head)


def _builtin_inline_sources(
    filesystem: _WorkspaceFilesystem | None = None,
) -> tuple[tuple[str, ExecutionSource], ...]:
    """Return the built-in Session inline sources installed in every Kernel."""

    return (
        (
            "cd",
            _InlineSource(
                "cd",
                prepare=_prepare_cd(filesystem),
                isolated=False,
            ),
        ),
        (
            "export",
            _InlineSource(
                "export",
                prepare=_prepare_export,
                isolated=False,
            ),
        ),
    )
