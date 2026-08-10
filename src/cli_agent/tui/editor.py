"""prompt_toolkit editor configuration for the cli-agent input box."""

from __future__ import annotations

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
