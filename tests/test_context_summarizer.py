import asyncio

from cli_agent.runtime import (
    AssistantMessage,
    ModelCompletion,
    ModelRequest,
    ScriptedModelProvider,
    SystemMessage,
    TextDelta,
    ToolCall,
    ToolCallReady,
    UserMessage,
)
from cli_agent.runtime._context_summarizer import (
    SUMMARY_SECTION_HEADERS,
    SummaryResult,
    _ContextSummarizer,
    build_summary_messages,
    has_all_summary_sections,
    summary_message,
)

FOUR_SECTION_SUMMARY = (
    "## Progress\nchecked the workspace\n"
    "## Files\nconfig.py edited\n"
    "## Todo\nrun the tests\n"
    "## Context\nuser prefers concise output"
)

PROMPT = (
    SystemMessage.text("Summary system instruction"),
    UserMessage.text("New transcript to merge into the summary:"),
    UserMessage.text("Some history"),
)


def _completion(text: str = FOUR_SECTION_SUMMARY) -> ModelCompletion:
    return ModelCompletion(
        message=AssistantMessage.text(text),
        finish_reason="stop",
    )


def _summarize(provider: ScriptedModelProvider) -> SummaryResult | None:
    return asyncio.run(_ContextSummarizer(provider).summarize(PROMPT))


def test_summarize_returns_a_four_section_message_without_tools() -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="Summarizing..."), _completion()),)
    )

    summary = _summarize(provider)

    assert summary is not None
    assert summary.message.content[0].text == FOUR_SECTION_SUMMARY
    assert provider.requests == (ModelRequest(messages=PROMPT, tools=()),)
    provider.assert_exhausted()


def test_summarize_fails_on_tool_call_ready_event() -> None:
    call = ToolCall(call_id="call_1", name="exec", arguments={"command": "ls"})
    provider = ScriptedModelProvider(script=((ToolCallReady(call=call),),))

    assert _summarize(provider) is None


def test_summarize_fails_on_completion_with_tool_calls() -> None:
    call = ToolCall(call_id="call_1", name="exec", arguments={"command": "ls"})
    provider = ScriptedModelProvider(
        script=(
            (
                ModelCompletion(
                    message=AssistantMessage(content=(call,)),
                    finish_reason="tool_calls",
                ),
            ),
        )
    )

    assert _summarize(provider) is None


def test_summarize_fails_without_a_completion() -> None:
    provider = ScriptedModelProvider(script=((TextDelta(text="partial"),),))

    assert _summarize(provider) is None


def test_summarize_fails_on_non_stop_finish_reason() -> None:
    provider = ScriptedModelProvider(
        script=(
            (
                ModelCompletion(
                    message=AssistantMessage.text(FOUR_SECTION_SUMMARY),
                    finish_reason="length",
                ),
            ),
        )
    )

    assert _summarize(provider) is None


def test_summarize_fails_on_provider_exception() -> None:
    class ExplodingProvider:
        async def generate(self, request: ModelRequest):
            raise RuntimeError("provider unavailable")
            yield

    summary = asyncio.run(_ContextSummarizer(ExplodingProvider()).summarize(PROMPT))

    assert summary is None


def test_build_summary_messages_includes_fixed_instruction_and_old_summary() -> None:
    messages = build_summary_messages(
        old_summary="## Progress\nold progress",
        delta=(UserMessage.text("New turn"),),
        max_tokens=1234,
    )

    assert isinstance(messages[0], SystemMessage)
    system_text = messages[0].content[0].text
    for section in SUMMARY_SECTION_HEADERS:
        assert section in system_text
    assert "untrusted data" in system_text
    assert "under 1234 tokens" in system_text
    assert messages[1] == UserMessage.text(
        "Previous summary to extend:\n## Progress\nold progress"
    )
    assert messages[2] == UserMessage.text("New transcript to merge into the summary:")
    assert messages[3:] == (UserMessage.text("New turn"),)


def test_build_summary_messages_omits_old_summary_when_absent() -> None:
    messages = build_summary_messages(
        old_summary=None,
        delta=(),
        max_tokens=100,
    )

    assert len(messages) == 2
    assert messages[1] == UserMessage.text("New transcript to merge into the summary:")


def test_has_all_summary_sections_requires_all_four() -> None:
    assert has_all_summary_sections(FOUR_SECTION_SUMMARY) is True
    assert has_all_summary_sections("## Progress\n## Files\n## Todo") is False
    assert has_all_summary_sections("") is False


def test_summary_message_uses_assistant_delimiter_projection() -> None:
    message = summary_message(FOUR_SECTION_SUMMARY)

    assert isinstance(message, AssistantMessage)
    assert message.content[0].text == (
        f"<session-summary>\n{FOUR_SECTION_SUMMARY}\n</session-summary>"
    )


def test_summarize_never_leaks_internal_text_deltas() -> None:
    provider = ScriptedModelProvider(
        script=((TextDelta(text="leaked?"), _completion()),)
    )

    summary = _summarize(provider)

    assert summary is not None
    assert summary.message.content[0].text == FOUR_SECTION_SUMMARY
    assert not summary.message.content[0].text.startswith("leaked?")
