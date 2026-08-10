"""prompt_toolkit editor configuration for the cli-agent input box."""

from __future__ import annotations

from prompt_toolkit.filters import has_completions
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys


def _build_key_bindings() -> KeyBindings:
    """Build the project-specific input bindings."""

    bindings = KeyBindings()

    @bindings.add(Keys.ControlJ)
    def _insert_newline(event) -> None:
        event.current_buffer.insert_text("\n")

    @bindings.add(Keys.ControlM)
    def _accept_input(event) -> None:
        event.current_buffer.validate_and_handle()

    @bindings.add(Keys.ControlI, filter=has_completions)
    def _accept_completion(event) -> None:
        buffer = event.current_buffer
        completion = buffer.complete_state.current_completion
        if completion is None and buffer.complete_state.completions:
            completion = buffer.complete_state.completions[0]
        if completion is not None:
            buffer.apply_completion(completion)
        buffer.cancel_completion()

    @bindings.add(Keys.Escape, filter=has_completions, eager=True)
    def _close_completion(event) -> None:
        event.current_buffer.cancel_completion()

    @bindings.add(Keys.ControlC)
    def _clear_or_interrupt(event) -> None:
        if event.current_buffer.text:
            event.current_buffer.reset()
            event.app.invalidate()
        else:
            event.app.exit(exception=KeyboardInterrupt())

    @bindings.add(Keys.ControlD)
    def _eof_or_delete(event) -> None:
        if event.current_buffer.text:
            event.current_buffer.delete()
        else:
            event.app.exit(exception=EOFError())

    return bindings
