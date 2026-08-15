"""Host-facing Runtime lifecycle with one active session binding.

The Runtime is the composition root and the owner of the active-session
state machine (RFC-0018): it never binds two Sessions at once. Durable
``Session`` data stays in the SessionStore; the Runtime owns the live
binding (ContextEngine + EnvironmentKernel + AgentLoop), the turn task
reference, and the lifecycle locks.

State transitions::

    NO_SESSION --new/resume--> REPLACING --attach--> ACTIVE_IDLE
    ACTIVE_IDLE --run_turn--> RUNNING_TURN --complete--> ACTIVE_IDLE
    ACTIVE_IDLE/RUNNING_TURN --new/resume--> REPLACING (cancel + join turn,
        detach current binding, attach target)
    ACTIVE_IDLE/RUNNING_TURN --detach--> NO_SESSION
    any non-terminal --close--> CLOSING --done--> CLOSED

Lifecycle operations are serialized by ``_lifecycle_op_lock``; the short
``_state_lock`` only protects the state and the turn-task reference, and
is never held across a long await.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any, Coroutine
from uuid import uuid4

from cli_agent.errors import (
    RuntimeStateError,
    SessionArchivedError,
    WorkspaceMismatchError,
    error_boundary,
)
from cli_agent.runtime._agent_loop import AgentLoop
from cli_agent.runtime._context import ContextEngineFactory, ContextPolicy, SessionUsage
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.interaction import UserInteraction
from cli_agent.runtime._environment.policy import ExecutionPolicy
from cli_agent.runtime._resources import (
    _reconcile_runtime_resources,
    _RuntimeResources,
)
from cli_agent.runtime._session import (
    ModelCallUsage,
    Session,
    SessionConfig,
    serialize_system_prompt,
)
from cli_agent.runtime._system_message import assemble_system_message
from cli_agent.runtime._turn import TurnStream
from cli_agent.runtime.diagnostic import RuntimeDiagnostic
from cli_agent.runtime.model import (
    AssistantMessage,
    ModelMessage,
    ModelProvider,
    ToolCall,
    UserMessage,
)


class RuntimeClosedError(RuntimeError):
    """Raised when work is requested from a closed Agent Runtime."""


class RuntimeState(str, Enum):
    """One state of the active-session state machine."""

    NO_SESSION = "no_session"
    REPLACING = "replacing"
    ACTIVE_IDLE = "active_idle"
    RUNNING_TURN = "running_turn"
    CLOSING = "closing"
    CLOSED = "closed"


# Predecessor states allowed for each target state.
_TRANSITIONS: dict[RuntimeState, tuple[RuntimeState, ...]] = {
    RuntimeState.REPLACING: (
        RuntimeState.NO_SESSION,
        RuntimeState.ACTIVE_IDLE,
        RuntimeState.RUNNING_TURN,
    ),
    RuntimeState.ACTIVE_IDLE: (RuntimeState.REPLACING,),
    RuntimeState.RUNNING_TURN: (RuntimeState.ACTIVE_IDLE,),
    RuntimeState.NO_SESSION: (RuntimeState.REPLACING,),
    RuntimeState.CLOSING: (
        RuntimeState.NO_SESSION,
        RuntimeState.ACTIVE_IDLE,
        RuntimeState.RUNNING_TURN,
    ),
    RuntimeState.CLOSED: (RuntimeState.CLOSING,),
}

_ATTACHABLE_STATES = (
    RuntimeState.NO_SESSION,
    RuntimeState.ACTIVE_IDLE,
    RuntimeState.RUNNING_TURN,
)

_ILLEGAL_TRANSITION_MESSAGE = "operation is not allowed in the current Runtime state"


@dataclass(slots=True)
class _ActiveBinding:
    """One live binding: durable Session plus its active-session resources.

    Locks, tasks, and closing flags never enter the Session data model;
    they live here, owned by the Runtime.
    """

    session: Session
    loop: AgentLoop
    kernel: EnvironmentKernel


class AgentRuntime:
    """Own Workspace-scoped resources and one active Session binding."""

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
        self._context_factory = ContextEngineFactory(
            store=resources.session_store,
            context_policy=context_policy,
            on_diagnostic=on_diagnostic,
        )
        self._state = RuntimeState.NO_SESSION
        self._binding: _ActiveBinding | None = None
        self._turn_task: asyncio.Task[Any] | None = None
        self._turn_stream: TurnStream | None = None
        self._lifecycle_op_lock = asyncio.Lock()
        self._state_lock = threading.Lock()
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
        backend: str = "local",
    ) -> Coroutine[Any, None, AgentRuntime]:
        """Validate arguments and return a coroutine that opens the Runtime.

        Awaiting the returned coroutine reconciles the Workspace, Capability
        View, Tool Catalog, and Tool Environment, then constructs the Runtime.
        The opened Runtime may also be used as an async context manager that
        closes itself on exit.

        Args:
            workspace (`str | Path`):
                Existing directory to bind as the Workspace (the Host
                control directory when ``backend`` is ``"docker"``).
            provider (`ModelProvider`):
                Model provider used by new Sessions when no per-session
                override is supplied to :meth:`new_session` or
                :meth:`resume_session`.
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
            backend (`str`):
                The Workspace Backend kind, fixed for the Workspace
                lifetime (``"local"`` or ``"docker"``); V1 does not
                hot-swap a Backend after open.

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
            backend=backend,
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
        backend: str,
    ) -> AgentRuntime:
        """Prepare Workspace-scoped resources and construct the Runtime."""

        resources = await _reconcile_runtime_resources(
            workspace=workspace,
            repertoire=repertoire,
            on_diagnostic=on_diagnostic,
            backend=backend,
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
            snapshot_library = resources.snapshot.library
            if snapshot_library is not None:
                snapshot_library.start(provider, on_diagnostic)
        except BaseException:
            with suppress(Exception):
                await resources.close()
            raise
        return runtime

    @property
    def closed(self) -> bool:
        """Return whether the Runtime has been closed."""

        return self._closed

    async def new_session(
        self,
        *,
        provider: ModelProvider | None = None,
    ) -> Session:
        """Create and attach one new durable Session.

        The current binding, if any, is detached first; its running turn
        (from another task) is cancelled and joined before the replacement
        proceeds. The attach-time system message is captured for audit in
        the SessionConfig.

        Args:
            provider (`ModelProvider | None`):
                Optional provider override bound to this Session; defaults
                to the Runtime provider.

        Returns:
            The created durable `Session`.

        Raises:
            RuntimeClosedError: If the Runtime is closed.
            RuntimeStateError: If the transition is not allowed.
        """

        async with self._lifecycle_op_lock:
            self._ensure_open()
            provider = provider if provider is not None else self._provider
            return await self._replace_active(
                self._attach_new(provider),
                action="new_session",
            )

    async def resume_session(
        self,
        session_id: str,
        *,
        provider: ModelProvider | None = None,
    ) -> Session:
        """Load, repair, and attach one durable Session.

        The current binding, if any, is detached first. The loaded Session
        must belong to this Workspace and must not be archived; the crash
        frontier is repaired before a fresh ContextEngine and Kernel are
        created, and the SystemMessage is rebuilt from the current
        Workspace / Capability environment instead of replaying the
        captured config.

        Args:
            session_id (`str`):
                The durable Session to resume.
            provider (`ModelProvider | None`):
                Optional provider override bound to this Session; defaults
                to the Runtime provider.

        Returns:
            The resumed durable `Session`.

        Raises:
            RuntimeClosedError: If the Runtime is closed.
            RuntimeStateError: If the transition is not allowed.
            HostFacingError: If the Session is missing, archived, belongs
                to another Workspace, or cannot be repaired.
        """

        async with self._lifecycle_op_lock:
            self._ensure_open()
            provider = provider if provider is not None else self._provider
            return await self._replace_active(
                self._attach_existing(session_id, provider),
                action="resume_session",
            )

    async def detach_session(self) -> None:
        """Detach and close the active binding, leaving ``NO_SESSION``.

        A running turn (from another task) is cancelled and joined before
        the Kernel closes. Detaching with no active Session is a no-op.

        Raises:
            RuntimeClosedError: If the Runtime is closed.
            RuntimeStateError: If the transition is not allowed.
        """

        async with self._lifecycle_op_lock:
            self._ensure_open()
            await self._cancel_active_turn(action="detach_session")
            await self._detach_current()
            self._transition_locked(RuntimeState.NO_SESSION, action="detach_session")

    async def archive_session(self, session_id: str) -> None:
        """Archive one durable Session; an active binding detaches first.

        Args:
            session_id (`str`): The Session to archive.

        Raises:
            RuntimeClosedError: If the Runtime is closed.
            HostFacingError: If the Session has no row or cannot be
                written.
        """

        async with self._lifecycle_op_lock:
            self._ensure_open()
            await self._detach_if_active(session_id, action="archive_session")
            self._resources.session_store.archive(session_id)

    async def unarchive_session(self, session_id: str) -> None:
        """Clear one durable Session's archive marker.

        Args:
            session_id (`str`): The Session to unarchive.

        Raises:
            RuntimeClosedError: If the Runtime is closed.
            HostFacingError: If the Session has no row or cannot be
                written.
        """

        async with self._lifecycle_op_lock:
            self._ensure_open()
            self._resources.session_store.unarchive(session_id)

    async def delete_session(self, session_id: str) -> None:
        """Delete one durable Session; an active binding detaches first.

        Args:
            session_id (`str`): The Session to delete.

        Raises:
            RuntimeClosedError: If the Runtime is closed.
            HostFacingError: If the Session has no row or cannot be
                written.
        """

        async with self._lifecycle_op_lock:
            self._ensure_open()
            await self._detach_if_active(session_id, action="delete_session")
            self._resources.session_store.delete(session_id)

    async def list_session_metadata(
        self,
        *,
        include_archived: bool = True,
    ) -> tuple[Session, ...]:
        """List durable Session metadata without exposing journal contents.

        Listing is a Host management operation. It is serialized with other
        lifecycle operations so the returned metadata reflects one coherent
        Store read, while it never binds or detaches a Session.
        """

        async with self._lifecycle_op_lock:
            self._ensure_open()
            return self._resources.session_store.list(
                include_archived=include_archived,
            )

    async def close(self) -> None:
        """Close all Runtime-owned resources idempotently.

        New lifecycle operations are rejected first, a running turn is
        cancelled and joined, the active binding closes its Kernel, then the
        Workspace-lifetime resources close in reverse dependency order:
        Library worker, Backend Workspace flush, Backend Workspace close.
        Every step is
        attempted even when an earlier close fails, so no resource leaks;
        the first failure is surfaced to the Host through a safe
        diagnostic and the original exception. The Runtime stays closed
        and a later close remains a no-op.
        """

        async with self._lifecycle_op_lock:
            with self._state_lock:
                if self._closed:
                    return
                self._closed = True
                self._transition_locked(RuntimeState.CLOSING, action="close")
                turn_task = self._turn_task
                turn_stream = self._turn_stream
                binding = self._binding
                current = asyncio.current_task()
            await self._cancel_and_join(turn_task)
            if turn_stream is not None and not turn_stream._terminal_queued:
                terminal_error = (
                    None
                    if (
                        turn_stream._consumer_task is current
                        or self._has_durable_tool_frontier(binding)
                    )
                    else asyncio.CancelledError()
                )
                turn_stream._put_terminal(terminal_error, force=True)
            errors: list[Exception] = []
            if binding is not None:
                try:
                    binding.loop.close()
                except Exception as exc:
                    errors.append(exc)
                try:
                    await binding.kernel.close()
                except Exception as exc:
                    errors.append(exc)
            try:
                await self._resources.close()
            except Exception as exc:
                errors.append(exc)
            with self._state_lock:
                self._transition_locked(RuntimeState.CLOSED, action="close")
                self._binding = None
                self._turn_task = None
                self._turn_stream = None
            if errors:
                self._emit_diagnostic(
                    "runtime.close_failed",
                    "Runtime close reported a failure",
                    detail={"exception": repr(errors[0])},
                )
                raise errors[0]

    def run_turn(self, message: UserMessage) -> TurnStream:
        """Run one model turn in the active Session.

        The Runtime must be in ``ACTIVE_IDLE``: a turn while another turn
        is already running is an illegal transition. The producer task is
        created before the stream is returned; the caller only consumes the
        stream and never owns the AgentLoop task.

        Args:
            message (`UserMessage`):
                User-authored message to append to the Session history.

        Returns:
            A bounded, consumer-only stream of Model events.

        Raises:
            RuntimeClosedError: If the Runtime is closed.
            RuntimeStateError: If there is no active Session or a turn is
                already running.
        """

        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._closed:
                raise RuntimeClosedError("AgentRuntime is closed")
            binding = self._binding
            if binding is None:
                raise RuntimeStateError(
                    action="run_turn",
                    state=self._state.value,
                    message=(
                        "no active session; call new_session or resume_session first"
                    ),
                )
            if self._state is not RuntimeState.ACTIVE_IDLE:
                raise RuntimeStateError(
                    action="run_turn",
                    state=self._state.value,
                    message=(
                        "a turn is already running or a lifecycle "
                        "operation is in progress"
                    ),
                )
            self._state = RuntimeState.RUNNING_TURN
            stream = TurnStream(
                queue=asyncio.Queue(maxsize=64),
                on_close=self._close_turn_stream,
                on_finish=self._finish_turn_stream,
            )
            task = loop.create_task(
                self._produce_turn(binding, message, stream),
                name="cli-agent-turn-producer",
            )
            self._turn_task = task
            self._turn_stream = stream
        return stream

    def session_usage(self) -> SessionUsage | None:
        """Return the active Session's cumulative usage, or ``None``.

        Returns:
            The session-cumulative input and output token counts, or
            ``None`` when no Session is bound (including after detach and
            close).
        """

        binding = self._binding
        if binding is None:
            return None
        return binding.loop.usage

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

    async def _replace_active(
        self,
        build: Coroutine[Any, Any, tuple[Session, _ActiveBinding]],
        *,
        action: str,
    ) -> Session:
        """Cancel, detach, build, and attach one new active binding.

        A failed build leaves the Runtime in ``NO_SESSION`` with no
        half-initialized binding and no leaked Kernel.
        """

        await self._cancel_active_turn(action=action)
        await self._detach_current()
        try:
            session, binding = await build
        except BaseException:
            self._transition_locked(RuntimeState.NO_SESSION, action=action)
            raise
        with self._state_lock:
            self._binding = binding
            self._transition_locked(RuntimeState.ACTIVE_IDLE, action=action)
        return session

    async def _cancel_active_turn(self, *, action: str) -> None:
        """Move to REPLACING and cancel the running turn, joining it.

        The producer task is cancelled and joined before the binding is
        detached; nothing is awaited while the state lock is held.
        """

        with self._state_lock:
            self._transition_locked(RuntimeState.REPLACING, action=action)
            turn_task = self._turn_task
            turn_stream = self._turn_stream
        await self._cancel_and_join(turn_task)
        if turn_stream is not None and not turn_stream._terminal_seen:
            turn_stream._put_terminal(asyncio.CancelledError(), force=True)
        with self._state_lock:
            if self._turn_task is turn_task:
                self._turn_task = None
            if self._turn_stream is turn_stream:
                self._turn_stream = None

    async def _cancel_and_join(self, task: asyncio.Task[Any] | None) -> None:
        """Cancel one producer and wait until its cleanup has completed."""

        if task is None:
            return
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task

    async def _produce_turn(
        self,
        binding: _ActiveBinding,
        message: UserMessage,
        stream: TurnStream,
    ) -> None:
        """Drive AgentLoop in the Runtime-owned producer task."""

        terminal_error: BaseException | None = None
        cancelled = False
        try:
            with error_boundary(
                "runtime.run_turn",
                on_diagnostic=self._boundary_diagnostic,
            ):
                async for event in binding.loop.run(message):
                    await stream._put_event(event)
        except asyncio.CancelledError:
            cancelled = True
            raise
        except BaseException as exc:
            terminal_error = exc
        finally:
            if not cancelled and terminal_error is not None and not stream._closed:
                stream._put_terminal(terminal_error)
            elif not cancelled and terminal_error is None and not stream._closed:
                stream._put_terminal(None)
            with self._state_lock:
                if self._turn_task is asyncio.current_task() and stream._closed:
                    self._turn_task = None
                    if self._turn_stream is stream:
                        self._turn_stream = None
                    if self._state is RuntimeState.RUNNING_TURN:
                        self._state = RuntimeState.ACTIVE_IDLE

    async def _close_turn_stream(self, stream: TurnStream) -> None:
        """Cancel a producer when a consumer abandons its stream."""

        with self._state_lock:
            if self._turn_stream is not stream:
                return
            turn_task = self._turn_task
        await self._cancel_and_join(turn_task)
        with self._state_lock:
            if self._turn_stream is stream:
                self._turn_stream = None
                self._turn_task = None
                if self._state is RuntimeState.RUNNING_TURN:
                    self._state = RuntimeState.ACTIVE_IDLE

    async def _finish_turn_stream(self, stream: TurnStream) -> None:
        """Join a normally terminal producer and release Runtime state."""

        with self._state_lock:
            if self._turn_stream is not stream:
                return
            turn_task = self._turn_task
        if turn_task is not None:
            with suppress(asyncio.CancelledError, Exception):
                await turn_task
        with self._state_lock:
            if self._turn_stream is stream:
                self._turn_stream = None
                self._turn_task = None
                if self._state is RuntimeState.RUNNING_TURN:
                    self._state = RuntimeState.ACTIVE_IDLE

    def _transition_locked(self, target: RuntimeState, *, action: str) -> None:
        """Transition to one state, rejecting illegal predecessors.

        The caller must hold ``_state_lock``; the check never awaits.
        """

        if self._state not in _TRANSITIONS[target]:
            raise RuntimeStateError(
                action=action,
                state=self._state.value,
                message=_ILLEGAL_TRANSITION_MESSAGE,
            )
        self._state = target

    async def _detach_current(self) -> None:
        """Close the current binding and drop the reference."""

        with self._state_lock:
            binding = self._binding
            self._binding = None
        if binding is None:
            return
        binding.loop.close()
        await binding.kernel.close()

    async def _detach_if_active(self, session_id: str, *, action: str) -> None:
        """Detach the active binding when it owns the given Session."""

        binding = self._binding
        if binding is not None and binding.session.session_id == session_id:
            await self._cancel_active_turn(action=action)
            await self._detach_current()
            self._transition_locked(RuntimeState.NO_SESSION, action=action)

    async def _attach_new(
        self,
        provider: ModelProvider,
    ) -> tuple[Session, _ActiveBinding]:
        """Create one durable Session and build its live binding."""

        workspace = self._resources.workspace
        session_id = uuid4().hex
        system = assemble_system_message(
            Path(workspace.root),
            self._instruction,
            snapshot=self._resources.snapshot,
        )
        session = self._resources.session_store.create(
            session_id,
            workspace.id,
            SessionConfig(serialize_system_prompt(system)),
        )
        binding = await self._build_binding(session, system, provider)
        return session, binding

    async def _attach_existing(
        self,
        session_id: str,
        provider: ModelProvider,
    ) -> tuple[Session, _ActiveBinding]:
        """Preflight, repair, and rebuild one durable Session's binding."""

        store = self._resources.session_store
        workspace = self._resources.workspace
        session, _ = store.load(session_id)
        if session.archived_at is not None:
            raise SessionArchivedError(session_id=session_id)
        if session.workspace_id != workspace.id:
            raise WorkspaceMismatchError(
                session_id=session_id,
                workspace_id=session.workspace_id,
                expected_workspace_id=workspace.id,
            )
        store.repair_interrupted_execution(
            session_id,
            expected_revision=session.revision,
        )
        session, _ = store.load(session_id)
        system = assemble_system_message(
            Path(workspace.root),
            self._instruction,
            snapshot=self._resources.snapshot,
        )
        binding = await self._build_binding(session, system, provider)
        return session, binding

    async def _build_binding(
        self,
        session: Session,
        system,
        provider: ModelProvider,
    ) -> _ActiveBinding:
        """Build the Kernel, ContextEngine, and AgentLoop for one Session.

        A construction failure closes the freshly created Kernel so a
        failed attach never leaks it.
        """

        kernel = self._new_kernel(session.session_id)
        try:
            context = self._context_factory.create(
                session.session_id,
                provider=provider,
                system_message=system,
            )
            store = self._resources.session_store

            def commit(message: ModelMessage) -> int:
                return store.append(
                    session.session_id,
                    message,
                    expected_revision=context.revision,
                )

            def commit_completion(
                message: ModelMessage,
                usage: ModelCallUsage | None,
            ) -> int:
                return store.append(
                    session.session_id,
                    message,
                    expected_revision=context.revision,
                    usage=usage,
                )

            loop = AgentLoop(
                provider,
                kernel,
                context=context,
                commit=commit,
                commit_completion=commit_completion,
                on_diagnostic=self._on_diagnostic,
            )
        except BaseException:
            with suppress(Exception):
                await kernel.close()
            raise
        return _ActiveBinding(session=session, loop=loop, kernel=kernel)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeClosedError("AgentRuntime is closed")

    def _new_kernel(self, session_id: str) -> EnvironmentKernel:
        return EnvironmentKernel(
            self._resources.workspace.root,
            backend=self._resources.backend,
            library_catalog=self._resources.snapshot.library,
            tool_catalog=self._resources.snapshot.tools,
            tool_executor=self._resources.tool_executor,
            base_env=self._resources.base_env,
            policy=self._policy,
            user_interaction=self._user_interaction,
            session_id=session_id,
            parallel_commands=self._parallel_commands,
            on_diagnostic=self._on_diagnostic,
        )

    @staticmethod
    def _has_durable_tool_frontier(binding: _ActiveBinding | None) -> bool:
        """Treat a post-tool-barrier close as an ordinary stream end.

        The Assistant tool call is already durable at this point. The
        interrupted execution frontier is therefore recoverable from the
        journal, while the consumer still receives no successful completion.
        """

        if binding is None:
            return False
        history = binding.loop.history
        if not history or not isinstance(history[-1], AssistantMessage):
            return False
        return any(isinstance(block, ToolCall) for block in history[-1].content)

    def _boundary_diagnostic(
        self,
        kind: str,
        message: str,
        detail: Mapping[str, object],
    ) -> None:
        """Sink used by ``error_boundary`` to emit boundary diagnostics."""

        self._emit_diagnostic(kind, message, detail=detail)

    def _emit_diagnostic(
        self,
        kind: str,
        message: str,
        *,
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
