"""Deterministic in-process Model Provider for scripted scenarios."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable

from cli_agent.runtime.model import ModelEvent, ModelRequest


class ScriptedModelProvider:
    """Yield one configured Model Event stream per generation request."""

    def __init__(self, script: Iterable[Iterable[ModelEvent]]) -> None:
        self._script = tuple(tuple(events) for events in script)
        self._next_stream = 0
        self._requests: list[ModelRequest] = []

    @property
    def requests(self) -> tuple[ModelRequest, ...]:
        """Return the Model Requests received so far."""

        return tuple(self._requests)

    async def generate(self, request: ModelRequest) -> AsyncIterator[ModelEvent]:
        """Record one request and yield its configured event stream in order."""

        self._requests.append(request)
        if self._next_stream >= len(self._script):
            raise RuntimeError(
                "ScriptedModelProvider received more model requests than scripted: "
                f"expected {len(self._script)}, received {len(self._requests)}"
            )

        events = self._script[self._next_stream]
        self._next_stream += 1
        for event in events:
            yield event

    def assert_exhausted(self) -> None:
        """Raise when one or more configured request streams remain unused."""

        remaining = len(self._script) - self._next_stream
        if remaining:
            raise RuntimeError(
                "ScriptedModelProvider received fewer model requests than scripted: "
                f"expected {len(self._script)}, received {len(self._requests)}; "
                f"{remaining} stream(s) remain"
            )
