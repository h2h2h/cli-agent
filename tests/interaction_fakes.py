"""Test-local UserInteraction fakes shared across Runtime contract tests."""

from __future__ import annotations

import asyncio

from cli_agent.runtime._environment.interaction import (
    UserAnswer,
    UserQuestion,
)


class _ScriptedInteraction:
    """Record every question and return one fixed answer."""

    def __init__(self, value: str | None) -> None:
        self._value = value
        self.questions: list[UserQuestion] = []

    async def ask(self, request: UserQuestion) -> UserAnswer:
        self.questions.append(request)
        return UserAnswer(value=self._value)


class _BlockingInteraction:
    """Never answer until released, and record cancellation."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.closed = False

    async def ask(self, request: UserQuestion) -> UserAnswer:
        del request
        self.entered.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return UserAnswer(value="allow_once")

    def close(self) -> None:
        self.closed = True


class _InvalidAnswerInteraction:
    async def ask(self, request: UserQuestion) -> object:
        del request
        return "not an answer"
