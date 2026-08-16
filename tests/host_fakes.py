"""Test HostServices composition helpers."""

from __future__ import annotations

from collections.abc import Callable

from cli_agent.runtime import CallbackEventSink, HostServices, RuntimeEvent, UserAnswer
from cli_agent.runtime._environment.kernel import EnvironmentKernel
from cli_agent.runtime.host import NULL_EVENTS, EventSink


class _UnavailableInteraction:
    async def ask(self, request: object) -> UserAnswer:
        del request
        return UserAnswer(value="deny")


def _environment_kernel(
    workspace: object,
    *args: object,
    interaction: object | None = None,
    events: EventSink | Callable[[RuntimeEvent], None] = NULL_EVENTS,
    host: HostServices | None = None,
    **kwargs: object,
) -> EnvironmentKernel:
    """Construct the real Kernel through an explicit test HostServices."""

    if host is None:
        sink = events if isinstance(events, EventSink) else CallbackEventSink(events)
        host = HostServices(
            interaction=interaction or _UnavailableInteraction(),  # type: ignore[arg-type]
            events=sink,
        )
    return EnvironmentKernel(
        workspace,  # type: ignore[arg-type]
        *args,
        host=host,
        **kwargs,  # type: ignore[arg-type]
    )
