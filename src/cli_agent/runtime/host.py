"""Host interaction and fire-and-forget Runtime event boundary."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, TypeAlias, runtime_checkable

from cli_agent.runtime.diagnostic import RuntimeDiagnostic

if TYPE_CHECKING:
    from cli_agent.runtime._environment.interaction import UserInteraction

RuntimeEvent: TypeAlias = RuntimeDiagnostic


@runtime_checkable
class EventSink(Protocol):
    """Receive one structured Runtime event synchronously."""

    def emit(self, event: RuntimeEvent) -> None:
        """Consume an event without returning control information."""
        ...


class NullEventSink:
    """Discard every Runtime event."""

    def emit(self, event: RuntimeEvent) -> None:
        del event


NULL_EVENTS: EventSink = NullEventSink()


class CallbackEventSink:
    """Adapt a Host callback to the explicit EventSink object boundary."""

    def __init__(self, callback: Callable[[RuntimeEvent], None]) -> None:
        self._callback = callback

    def emit(self, event: RuntimeEvent) -> None:
        self._callback(event)


class _SafeEventSink:
    """Prevent observability adapter failures crossing the Runtime boundary."""

    def __init__(self, inner: EventSink) -> None:
        self._inner = inner

    def emit(self, event: RuntimeEvent) -> None:
        try:
            self._inner.emit(event)
        except Exception:
            pass


@dataclass(frozen=True, slots=True, init=False)
class HostServices:
    """The Host's bidirectional interaction and unidirectional event ports."""

    interaction: UserInteraction
    events: EventSink

    def __init__(
        self,
        *,
        interaction: UserInteraction,
        events: EventSink | None = None,
    ) -> None:
        object.__setattr__(self, "interaction", interaction)
        object.__setattr__(
            self,
            "events",
            _SafeEventSink(events or NullEventSink()),
        )


def emit_event(events: EventSink, event: RuntimeEvent) -> None:
    """Emit defensively even when a test adapter bypasses HostServices."""

    try:
        events.emit(event)
    except Exception:
        pass
