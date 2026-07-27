"""Terminal presentation for provider-neutral model events."""

from __future__ import annotations

from typing import TextIO

from cli_agent.runtime import (
    ModelCompletion,
    ModelEvent,
    TextDelta,
    ToolCallReady,
)

_RESET = "\033[0m"
_PROMPT_STYLE = "\033[1;36m"
_TOOL_STYLE = "\033[1;35m"
_COMMAND_STYLE = "\033[33m"
_COMPLETION_STYLE = "\033[2;32m"


def render_prompt(*, stderr: TextIO) -> None:
    """Render the interactive prompt."""

    prompt = _styled("cli-agent> ", _PROMPT_STYLE, stream=stderr)
    stderr.write(prompt)
    stderr.flush()


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
        diagnostic = _styled(
            f"[tool] {event.call.name}",
            _TOOL_STYLE,
            stream=stderr,
        )
        command = event.call.arguments.get("command")
        if event.call.name == "exec" and isinstance(command, str):
            diagnostic += f": {_styled(command, _COMMAND_STYLE, stream=stderr)}"
        print(diagnostic, file=stderr, flush=True)
        return

    if isinstance(event, ModelCompletion):
        diagnostic = f"[completion] reason={event.finish_reason}"
        if event.usage is not None:
            diagnostic += (
                f" usage=input:{event.usage.input_tokens}"
                f",output:{event.usage.output_tokens}"
                f",total:{event.usage.total_tokens}"
            )
        print(
            _styled(diagnostic, _COMPLETION_STYLE, stream=stderr),
            file=stderr,
            flush=True,
        )
        return

    raise TypeError(f"unsupported model event: {type(event).__name__}")


def _styled(text: str, style: str, *, stream: TextIO) -> str:
    if not stream.isatty():
        return text
    return f"{style}{text}{_RESET}"
