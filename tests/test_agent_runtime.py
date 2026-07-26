import asyncio
from pathlib import Path

import pytest

import runtime.runtime as runtime_module
from runtime import AgentRuntime, RuntimeClosedError, ScriptedModelProvider


def test_opens_and_closes_runtime_explicitly(tmp_path: Path, monkeypatch) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )

    async def scenario() -> None:
        runtime = await AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
        )

        assert not runtime.closed
        assert len(_TrackingEnvironmentKernel.instances) == 1

        await runtime.close()
        await runtime.close()

        assert runtime.closed
        assert _TrackingEnvironmentKernel.instances[0].close_count == 1
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            async with runtime:
                pass

    asyncio.run(scenario())


def test_closes_runtime_context_manager(tmp_path: Path, monkeypatch) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )

    async def scenario() -> None:
        opener = AgentRuntime.open(
            workspace=tmp_path,
            provider=ScriptedModelProvider(script=()),
        )
        async with opener as runtime:
            assert not runtime.closed

        assert runtime.closed
        assert len(_TrackingEnvironmentKernel.instances) == 1
        assert _TrackingEnvironmentKernel.instances[0].close_count == 1
        with pytest.raises(RuntimeClosedError, match="AgentRuntime is closed"):
            async with opener:
                pass

    asyncio.run(scenario())


def test_cleans_up_environment_when_open_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _TrackingEnvironmentKernel.instances.clear()
    monkeypatch.setattr(
        runtime_module,
        "EnvironmentKernel",
        _TrackingEnvironmentKernel,
    )

    class FailingAgentRuntime(AgentRuntime):
        def __init__(self, **kwargs: object) -> None:
            raise OpenFailure

    async def scenario() -> None:
        with pytest.raises(OpenFailure):
            await FailingAgentRuntime.open(
                workspace=tmp_path,
                provider=ScriptedModelProvider(script=()),
            )

        assert len(_TrackingEnvironmentKernel.instances) == 1
        assert _TrackingEnvironmentKernel.instances[0].close_count == 1

    asyncio.run(scenario())


class OpenFailure(Exception):
    pass


class _TrackingEnvironmentKernel:
    instances: list["_TrackingEnvironmentKernel"] = []

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace)
        self.close_count = 0
        self.instances.append(self)

    async def close(self) -> None:
        self.close_count += 1
