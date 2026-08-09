"""Project-owned asynchronous terminal input session."""

from __future__ import annotations

import asyncio
import sys
from typing import TextIO

from prompt_toolkit import PromptSession
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_input
from prompt_toolkit.output import create_output

from .editor import _build_key_bindings

__all__ = ["TuiSession"]


class TuiSession:
    """Own one asynchronous prompt_toolkit input session."""

    def __init__(
        self,
        *,
        stdin: TextIO | None = None,
        stderr: TextIO | None = None,
    ) -> None:
        input_stream = sys.stdin if stdin is None else stdin
        output_stream = sys.stderr if stderr is None else stderr

        self._input = create_input(input_stream)
        self._output = create_output(output_stream)
        self._prompt = PromptSession(
            input=self._input,
            output=self._output,
            history=InMemoryHistory(),
            key_bindings=_build_key_bindings(),
            multiline=True,
        )
        self._lock = asyncio.Lock()
        self._active_task: asyncio.Task[object] | None = None
        self._closed = False

    async def read_text(self, prompt: str) -> str | None:
        """Read one submitted text value, returning ``None`` on EOF."""

        return await self._read(prompt)

    async def confirm(self, prompt: str) -> bool:
        """Read a confirmation and allow only an explicit yes answer."""

        response = await self._read(prompt)
        return response is not None and response.strip().casefold() in {
            "y",
            "yes",
        }

    async def close(self) -> None:
        """Close the input resource and make the session unusable."""

        if self._closed:
            return

        self._closed = True
        active_task = self._active_task
        current_task = asyncio.current_task()
        if active_task is not None and active_task is not current_task:
            active_task.cancel()

        async with self._lock:
            self._input.close()

    async def _read(self, prompt: str) -> str | None:
        async with self._lock:
            if self._closed:
                raise RuntimeError("TuiSession is closed")

            self._active_task = asyncio.current_task()
            try:
                return await self._prompt.prompt_async(
                    prompt,
                    handle_sigint=False,
                )
            except EOFError:
                return None
            finally:
                self._active_task = None
