"""Runner integration tests for TTY Markdown streaming."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from io import StringIO
from pathlib import Path

import pytest

import cli_agent.runner as runner_module
from cli_agent.config import CliConfig
from cli_agent.runner import run_agent
from cli_agent.runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelEvent,
    ScriptedModelProvider,
    TextDelta,
)


class _TtyOutput(StringIO):
    def isatty(self) -> bool:
        return True


class _FakeRenderer:
    instances: list[_FakeRenderer] = []

    def __init__(self, stdout: object) -> None:
        self.stdout = stdout
        self.feed_text: list[str] = []
        self.suspend_count = 0
        self.finish_count = 0
        self.instances.append(self)

    def feed(self, text: str) -> None:
        self.feed_text.append(text)

    def suspend(self) -> None:
        self.suspend_count += 1

    def resume(self) -> None:
        pass

    def finish(self) -> None:
        self.finish_count += 1


def test_tty_one_shot_turn_uses_and_finishes_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_renderer(monkeypatch)
    provider = _completed_provider("Done.")
    stdout = _TtyOutput()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task="Run once"),
            provider,
            stdin=StringIO(),
            stdout=stdout,
            stderr=StringIO(),
        )
    )

    assert exit_code == 0
    renderer = _FakeRenderer.instances[0]
    assert renderer.feed_text == ["Done."]
    assert renderer.suspend_count == 1
    assert renderer.finish_count == 1
    assert stdout.getvalue() == ""
    provider.assert_exhausted()


def test_tty_interactive_turn_does_not_add_legacy_newline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_renderer(monkeypatch)
    provider = _completed_provider("Done.")
    stdout = _TtyOutput()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task=None),
            provider,
            stdin=StringIO("work\n:q\n"),
            stdout=stdout,
            stderr=StringIO(),
        )
    )

    assert exit_code == 0
    renderer = _FakeRenderer.instances[0]
    assert renderer.feed_text == ["Done."]
    assert renderer.finish_count == 1
    assert stdout.getvalue() == ""
    provider.assert_exhausted()


def test_renderer_finishes_when_turn_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_renderer(monkeypatch)

    class RaisingRuntime:
        def run_turn(self, message: object) -> AsyncIterator[ModelEvent]:
            del message

            async def events() -> AsyncIterator[ModelEvent]:
                yield TextDelta(text="partial")
                raise RuntimeError("turn failed")

            return events()

    with pytest.raises(RuntimeError, match="turn failed"):
        asyncio.run(
            runner_module._run_turn(
                RaisingRuntime(),
                "work",
                stdout=_TtyOutput(),
                stderr=StringIO(),
                separate_diagnostics=False,
            )
        )

    renderer = _FakeRenderer.instances[0]
    assert renderer.feed_text == ["partial"]
    assert renderer.finish_count == 1


def test_renderer_construction_failure_falls_back_to_raw_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_construct(stdout: object) -> object:
        del stdout
        raise OSError("terminal size unavailable")

    monkeypatch.setattr(runner_module, "MarkdownStreamRenderer", fail_to_construct)
    provider = _completed_provider("Done.")
    stdout = _TtyOutput()

    exit_code = asyncio.run(
        run_agent(
            _config(tmp_path, task="Run once"),
            provider,
            stdin=StringIO(),
            stdout=stdout,
            stderr=StringIO(),
        )
    )

    assert exit_code == 0
    assert stdout.getvalue() == "Done."
    provider.assert_exhausted()


def test_non_tty_does_not_create_renderer() -> None:
    assert runner_module._create_markdown_renderer(StringIO()) is None


def _install_fake_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeRenderer.instances.clear()
    monkeypatch.setattr(runner_module, "MarkdownStreamRenderer", _FakeRenderer)


def _completed_provider(text: str) -> ScriptedModelProvider:
    return ScriptedModelProvider(
        script=(
            (
                TextDelta(text=text),
                ModelCompletion(
                    message=AssistantMessage.text(text),
                    finish_reason="stop",
                ),
            ),
        )
    )


def _config(tmp_path: Path, *, task: str | None) -> CliConfig:
    return CliConfig(
        task=task,
        workspace=tmp_path,
        base_url="https://models.example/v1",
        model="test-model",
        api_key="secret",
        repertoire=None,
        context_window_tokens=128_000,
        output_reserve_tokens=4_000,
        safety_margin_tokens=4_096,
    )
