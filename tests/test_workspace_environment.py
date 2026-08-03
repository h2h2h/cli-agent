import asyncio
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction

import cli_agent.runtime.runtime as runtime_module
from cli_agent.runtime import AgentRuntime, ScriptedModelProvider

_user_interaction = _ScriptedInteraction("allow_once")



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
        runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
        )

        assert dict(runtime._resources.base_env) == {
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
            runtime._resources.base_env["NEW"] = "value"  # type: ignore[index]
        await runtime.close()

    asyncio.run(scenario())


def test_workspace_environment_is_loaded_once_per_runtime(tmp_path: Path) -> None:
    environment = tmp_path / ".workspace" / "env"
    environment.parent.mkdir()
    environment.write_text("VALUE=first\n", encoding="utf-8")

    async def scenario() -> None:
        first_runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
        )
        environment.write_text("VALUE=second\n", encoding="utf-8")
        second_runtime = await AgentRuntime.open(
            user_interaction=_user_interaction,
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
        )

        assert first_runtime._resources.base_env == {"VALUE": "first"}
        assert second_runtime._resources.base_env == {"VALUE": "second"}
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
            await AgentRuntime.open(
                user_interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
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
            await AgentRuntime.open(
                user_interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
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
            await AgentRuntime.open(
                user_interaction=_user_interaction,
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
            )
        assert "SECOND BAD LINE" not in str(raised.value)

    asyncio.run(scenario())
