"""Slash command completion for the interactive input box."""

from __future__ import annotations

from collections.abc import Iterable

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document

from cli_agent.slash_commands import CommandSpec


class _SlashCommandCompleter(Completer):
    """Offer slash command candidates while the user types."""

    def __init__(self, specs: tuple[CommandSpec, ...]) -> None:
        self._specs = specs

    def get_completions(
        self,
        document: Document,
        complete_event: CompleteEvent,
    ) -> Iterable[Completion]:
        fragment = document.text_before_cursor
        if not _is_command_fragment(fragment):
            return

        query = fragment[1:].casefold()
        for spec in self._specs:
            if spec.name.casefold().startswith(query):
                yield Completion(
                    text=f"/{spec.name}",
                    start_position=-len(fragment),
                    display=f"/{spec.name}",
                    display_meta=spec.description,
                )


def _is_command_fragment(fragment: str) -> bool:
    """Return True when the text before the cursor starts a slash command."""

    return fragment.startswith("/") and not any(char.isspace() for char in fragment[1:])
