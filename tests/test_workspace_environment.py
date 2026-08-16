import asyncio
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

import cli_agent.runtime.runtime as runtime_module
from cli_agent.presets import open_default_runtime
from cli_agent.runtime import (
    ContextPolicy,
    ScriptedModelProvider,
)

_user_interaction = _ScriptedInteraction("allow_once")
_context_policy = ContextPolicy(
    context_window_tokens=16_384,
    output_reserve_tokens=2_048,
    safety_margin_tokens=0,
)


def test_runtime_open_loads_complete_dotenv_environment(
    tmp_path: Path,
) -> None:
    environment = tmp_path / ".workspace" / "env"
    environment.parent.mkdir()
    environment.write_text(
        "\n".join(
            (
                "# Workspace custom environment",
                'API_BASE_URL=" https://example.test/api "',
                "EMPTY=",
                r'MULTILINE="first\nsecond"',
                "_PRIVATE_2=value",
                "export EXPORTED=yes",
                "DUPLICATE=first",
                "DUPLICATE=second",
                "VALUE=upper",
                "value=lower",
                "LITERAL=${UNRESOLVED}",
                "",
            )
        ),
        encoding="utf-8",
    )

    async def scenario() -> None:
        runtime = await open_default_runtime(
            interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )

        assert dict(runtime._resources.workspace.base_environment) == {
            "API_BASE_URL": " https://example.test/api ",
            "DUPLICATE": "second",
            "EMPTY": "",
            "EXPORTED": "yes",
            "LITERAL": "${UNRESOLVED}",
            "MULTILINE": "first\nsecond",
            "VALUE": "upper",
            "_PRIVATE_2": "value",
            "value": "lower",
        }
        with pytest.raises(TypeError):
            runtime._resources.workspace.base_environment["NEW"] = "value"  # type: ignore[index]
        await runtime.close()

    asyncio.run(scenario())


def test_workspace_environment_is_loaded_once_per_runtime(tmp_path: Path) -> None:
    environment = tmp_path / ".workspace" / "env"
    environment.parent.mkdir()
    environment.write_text("VALUE=first\n", encoding="utf-8")

    async def scenario() -> None:
        first_runtime = await open_default_runtime(
            interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )
        environment.write_text("VALUE=second\n", encoding="utf-8")
        second_runtime = await open_default_runtime(
            interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
            context_policy=_context_policy,
        )

        assert first_runtime._resources.workspace.base_environment == {"VALUE": "first"}
        assert second_runtime._resources.workspace.base_environment == {"VALUE": "second"}
        await first_runtime.close()
        await second_runtime.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (b"SECRET ???\n", "invalid dotenv syntax"),
        (b"MISSING_VALUE\n", "must use KEY=VALUE"),
        (b"INVALID_UTF8=\xff\n", "must contain valid UTF-8"),
        (b"CONTAINS_NUL=secret\x00value\n", "must not contain NUL"),
    ),
)
def test_runtime_open_rejects_malformed_workspace_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    message: str,
) -> None:
    environment = tmp_path / ".workspace" / "env"
    environment.parent.mkdir()
    environment.write_bytes(payload)

    class UnexpectedEnvironmentKernel:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("EnvironmentKernel must not be constructed")

    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        UnexpectedEnvironmentKernel,
    )

    async def scenario() -> None:
        with pytest.raises(ValueError, match=message) as raised:
            await open_default_runtime(
                interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
                context_policy=_context_policy,
            )
        assert "secret" not in str(raised.value).lower()

    asyncio.run(scenario())


def test_runtime_open_rejects_workspace_environment_symbolic_link(
    tmp_path: Path,
) -> None:
    state = tmp_path / ".workspace"
    state.mkdir()
    target = tmp_path / "target"
    target.write_text("TOKEN=secret\n", encoding="utf-8")
    environment = state / "env"
    try:
        environment.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symbolic links are unavailable: {exc}")

    async def scenario() -> None:
        with pytest.raises(
            ValueError,
            match="must be a real regular file",
        ) as raised:
            await open_default_runtime(
                interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
                context_policy=_context_policy,
            )
        assert "secret" not in str(raised.value)

    asyncio.run(scenario())


def test_runtime_open_reports_first_invalid_dotenv_line(tmp_path: Path) -> None:
    environment = tmp_path / ".workspace" / "env"
    environment.parent.mkdir()
    environment.write_text(
        "VALID=before\nFIRST BAD LINE\nSECOND BAD LINE\n",
        encoding="utf-8",
    )

    async def scenario() -> None:
        with pytest.raises(ValueError, match="at line 2") as raised:
            await open_default_runtime(
                interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
                context_policy=_context_policy,
            )
        assert "SECOND BAD LINE" not in str(raised.value)

    asyncio.run(scenario())
