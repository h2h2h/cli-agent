"""Host-facing Runtime lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine, Mapping
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from cli_agent.runtime._agent_loop import AgentLoop
from cli_agent.runtime._context import ContextPolicy
from cli_agent.runtime._context_manager import SessionUsage
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.interaction import UserInteraction
from cli_agent.runtime._environment.policy import ExecutionPolicy
from cli_agent.runtime._resources import (
    _reconcile_runtime_resources,
    _RuntimeResources,
)
from cli_agent.runtime._session_history import serialize_system_prompt
from cli_agent.runtime._system_message import assemble_system_message
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.model import ModelEvent, ModelProvider, UserMessage


class RuntimeClosedError(RuntimeError):
    """Raised when work is requested from a closed Agent Runtime."""


@dataclass(slots=True)
class _Session:
    kernel: EnvironmentKernel
    loop: AgentLoop
    lock: asyncio.Lock
    closing: bool = False
    active_task: asyncio.Task[Any] | None = None


class AgentRuntime:
    """Own Workspace-scoped resources for the host."""

    def __init__(
        self,
        *,
        provider: ModelProvider,
        resources: _RuntimeResources,
        policy: ExecutionPolicy | None,
        user_interaction: UserInteraction,
        parallel_commands: frozenset[str],
        instruction: str | None,
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None,
        context_policy: ContextPolicy,
    ) -> None:
        self._provider = provider
        self._resources = resources
        self._policy = policy
        self._user_interaction = user_interaction
        self._parallel_commands = parallel_commands
        self._instruction = instruction
        self._on_diagnostic = on_diagnostic
        self._context_policy = context_policy
        self._sessions: dict[str, _Session] = {}
        self._closed = False

    @classmethod
    def open(
        cls,
        *,
        workspace: str | Path,
        provider: ModelProvider,
        user_interaction: UserInteraction,
        context_policy: ContextPolicy,
        repertoire: str | Path | None = None,
        system_instruction: str | None = None,
        execution_policy: ExecutionPolicy | None = None,
        parallel_commands: frozenset[str] | None = None,
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None = None,
    ) -> Coroutine[Any, None, AgentRuntime]:
        """Validate arguments and return a coroutine that opens the Runtime.

        Awaiting the returned coroutine reconciles the Workspace, Capability
        View, Tool Catalog, and Tool Environment, then constructs the Runtime.
        The opened Runtime may also be used as an async context manager that
        closes itself on exit.

        Args:
            workspace (`str | Path`):
                Existing directory to bind as the Workspace.
            provider (`ModelProvider`):
                Model provider used by new Sessions when no per-turn override
                is supplied to :meth:`run_turn`.
            user_interaction (`UserInteraction`):
                Host-owned Runtime-wide question channel; required even when
                ``execution_policy`` is omitted.
            context_policy (`ContextPolicy`):
                Explicit Context budget and compaction policy for every
                Session; there is no implicit model-name window registry.
            repertoire (`str | Path | None`):
                User-maintained capability lower tree; defaults to
                ``~/.cli-agent/repertoire``.
            system_instruction (`str | None`):
                Optional Host instruction appended to the canonical per-Session
                system message.
            execution_policy (`ExecutionPolicy | None`):
                Optional Host-injected execution Policy. ``None`` fully skips
                Policy evaluation; no default Policy or implicit decision is
                constructed.
            parallel_commands (`frozenset[str] | None`):
                Executable basenames trusted to run in parallel Shell batches.
            on_diagnostic (`Callable[[RuntimeDiagnostic], None] | None`):
                Optional Host callback receiving structured Runtime
                Diagnostics, such as MCP discovery exhaustion, without blocking
                Runtime open. Omitted callbacks keep today's silent behavior.

        Returns:
            A coroutine resolving to the opened :class:`AgentRuntime`.

        """

        return cls._reconcile(
            workspace=workspace,
            repertoire=repertoire,
            provider=provider,
            instruction=system_instruction,
            policy=execution_policy,
            user_interaction=user_interaction,
            parallel_commands=frozenset(parallel_commands or ()),
            on_diagnostic=on_diagnostic,
            context_policy=context_policy,
        )

    @classmethod
    async def _reconcile(
        cls,
        *,
        workspace: str | Path,
        repertoire: str | Path | None,
        provider: ModelProvider,
        instruction: str | None,
        policy: ExecutionPolicy | None,
        user_interaction: UserInteraction,
        parallel_commands: frozenset[str],
        on_diagnostic: Callable[[RuntimeDiagnostic], None] | None,
        context_policy: ContextPolicy,
    ) -> AgentRuntime:
        """Prepare Workspace-scoped resources and construct the Runtime."""

        resources = await _reconcile_runtime_resources(
            workspace=workspace,
            repertoire=repertoire,
            on_diagnostic=on_diagnostic,
        )
        try:
            runtime = cls(
                provider=provider,
                resources=resources,
                policy=policy,
                user_interaction=user_interaction,
                parallel_commands=parallel_commands,
                instruction=instruction,
                on_diagnostic=on_diagnostic,
                context_policy=context_policy,
            )
            resources.library_catalog.start(provider, on_diagnostic)
        except BaseException:
            with suppress(Exception):
                await resources.close()
            raise
        return runtime

    @property
    def closed(self) -> bool:
        """Return whether the Runtime has been closed."""

        return self._closed

    async def close(self) -> None:
        """Close all Runtime-owned resources idempotently.

        New turns are rejected first, every Session Kernel (and its queued and
        running Executions) is closed, then the Workspace-lifetime resources
        close in reverse dependency order: Library worker, Backend Workspace
        flush, Backend Workspace close. Every step is attempted even when an
        earlier close fails, so no resource leaks; the first failure is
        surfaced to the Host through a safe diagnostic and the original
        exception. The Runtime stays closed and a later close remains a no-op.
        """

        if self._closed:
            return
        self._closed = True
        sessions = tuple(self._sessions.values())
        self._sessions.clear()
        errors: list[Exception] = []
        for session in sessions:
            try:
                await self._close_session_state(session)
            except Exception as exc:
                errors.append(exc)
        try:
            await self._resources.close()
        except Exception as exc:
            errors.append(exc)
        if errors:
            self._emit_diagnostic(
                "runtime.close_failed",
                "Runtime close reported a failure",
                detail={"exception": repr(errors[0])},
            )
            raise errors[0]

    async def run_turn(
        self,
        session_id: str,
        message: UserMessage,
        *,
        provider: ModelProvider | None = None,
    ) -> AsyncIterator[ModelEvent]:
        """Run one turn in a get-or-created Agent Session.

        Args:
            session_id (`str`):
                Host-visible Session identifier. Reusing an id that was closed
                creates a fresh Loop and Kernel.
            message (`UserMessage`):
                User-authored message to append to the Session history.
            provider (`ModelProvider | None`):
                Optional provider override; used only when ``session_id`` is
                first seen.

        Yields:
            Model events streamed by the Session's Agent Loop.
        """

        self._ensure_open()
        session = self._sessions.get(session_id)
        if session is None:
            bound_provider = provider if provider is not None else self._provider
            system = assemble_system_message(
                self._resources.workspace,
                self._instruction,
                tool_catalog=self._resources.tool_catalog,
                skill_catalog=self._resources.skill_catalog,
            )
            self._resources.session_history.begin_session(
                session_id,
                str(self._resources.workspace),
                serialize_system_prompt(system),
            )
            kernel = self._new_kernel(session_id)
            try:
                loop = AgentLoop(
                    bound_provider,
                    kernel,
                    system_message=system,
                    context_policy=self._context_policy,
                    session_id=session_id,
                    on_diagnostic=self._on_diagnostic,
                    on_append=(
                        lambda message: self._resources.session_history.append(
                            session_id, message
                        )
                    ),
                )
            except BaseException:
                await kernel.close()
                raise
            candidate = _Session(
                kernel=kernel,
                loop=loop,
                lock=asyncio.Lock(),
            )
            session = self._sessions.setdefault(session_id, candidate)
            if session is not candidate:
                await kernel.close()

        async with session.lock:
            self._ensure_open()
            if session.closing:
                raise RuntimeClosedError("Agent Session is closed")
            active_task = asyncio.current_task()
            session.active_task = active_task
            try:
                async for event in session.loop.run(message):
                    if self._closed or session.closing:
                        return
                    yield event
                    if self._closed or session.closing:
                        return
            finally:
                if session.active_task is active_task:
                    session.active_task = None

    async def close_session(self, session_id: str) -> None:
        """Close and forget one Agent Session idempotently."""

        self._resources.session_history.close_session(session_id)
        session = self._sessions.pop(session_id, None)
        if session is not None:
            await self._close_session_state(session)

    def session_usage(self, session_id: str) -> SessionUsage | None:
        """Return the session-cumulative token usage, or ``None`` when unknown.

        Args:
            session_id (`str`):
                Host-visible Session identifier.

        Returns:
            The session-cumulative input and output token counts, or ``None``
            when no open Session with this id exists, including closed ones.
        """

        session = self._sessions.get(session_id)
        if session is None:
            return None
        return session.loop.usage

    async def __aenter__(self) -> AgentRuntime:
        self._ensure_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeClosedError("AgentRuntime is closed")

    def _new_kernel(self, session_id: str) -> EnvironmentKernel:
        return EnvironmentKernel(
            self._resources.workspace,
            backend=self._resources.backend,
            library_catalog=self._resources.library_catalog,
            tool_catalog=self._resources.tool_catalog,
            base_env=self._resources.base_env,
            policy=self._policy,
            user_interaction=self._user_interaction,
            session_id=session_id,
            parallel_commands=self._parallel_commands,
            on_diagnostic=self._on_diagnostic,
        )

    async def _close_session_state(self, session: _Session) -> None:
        """Cancel one active turn, stop its Kernel, and await its unwind."""

        session.closing = True
        active_task = session.active_task
        current_task = asyncio.current_task()
        if (
            active_task is not None
            and active_task is not current_task
            and not active_task.done()
        ):
            active_task.cancel()

        error: Exception | None = None
        try:
            await session.kernel.close()
        except Exception as exc:
            error = exc

        if active_task is not None and active_task is not current_task:
            with suppress(asyncio.CancelledError, Exception):
                await active_task
        if active_task is not current_task:
            async with session.lock:
                pass

        if error is not None:
            raise error

    def _emit_diagnostic(
        self,
        kind: str,
        message: str,
        detail: Mapping[str, object] | None = None,
    ) -> None:
        """Emit one structured notice when a Host callback is configured.

        Args:
            kind (`str`):
                Stable diagnostic category, for example
                ``mcp.discovery_failed``.
            message (`str`):
                Human-readable summary for the Host to log or present.
            detail (`Mapping[str, object] | None`):
                Optional structured detail; never contains env values,
                credentials, or Secret References.
        """

        if self._on_diagnostic is None:
            return
        self._on_diagnostic(
            RuntimeDiagnostic(
                kind=kind,
                message=message,
                detail=detail or {},
            )
        )
