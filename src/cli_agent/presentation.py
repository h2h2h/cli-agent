"""Terminal presentation for provider-neutral model events."""

from __future__ import annotations

from typing import TextIO

from cli_agent.runtime import (
    ModelCompletion,
    ModelEvent,
    TextDelta,
    ToolCallReady,
)


def render_event(
    event: ModelEvent,
    *,
    stdout: TextIO,
    stderr: TextIO,
) -> None:
    """Render one provider-neutral event."""

    if isinstance(event, TextDelta):
        stdout.write(event.text)
        stdout.flush()
        return

    if isinstance(event, ToolCallReady):
        print(f"[tool] {event.call.name}", file=stderr, flush=True)
        return

    if isinstance(event, ModelCompletion):
        diagnostic = f"[completion] reason={event.finish_reason}"
        if event.usage is not None:
            diagnostic += (
                f" usage=input:{event.usage.input_tokens}"
                f",output:{event.usage.output_tokens}"
                f",total:{event.usage.total_tokens}"
            )
        print(diagnostic, file=stderr, flush=True)
        return

    raise TypeError(f"unsupported model event: {type(event).__name__}")
