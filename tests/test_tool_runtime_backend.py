"""Issue 08: Tool Runtime runs inside the Backend Workspace.

These tests pin the acceptance criteria of RFC-0012 issue 08: the Tool worker
shares the Backend Workspace and cwd with Shell/Files, the Handler only
produces backend-neutral Tool requests (no Host Python, worker path, Tool
path, or ``os.environ``), dependency failure stays fail-soft without a Host
Python fallback, and Command Handlers never create Host subprocesses.
"""

import asyncio
import importlib
import json
from pathlib import Path

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._backend import _ToolBinding, _ToolExecutionRequest
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalCapabilityView,
    _ProcessExecution,
)
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.handlers.executions import _InlineExecution


def _repertoire(workspace: Path) -> Path:
    repertoire = workspace.parent / f"{workspace.name}-repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    return repertoire


def test_prepare_tool_payload_is_composed_by_the_backend(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "math.py").write_text("def add(a, b):\n    return a + b\n")
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    backend = _LocalBackendWorkspace(tmp_path, {}, view)

    async def scenario() -> None:
        status = await backend.reconcile_tool_runtime()
        assert status.available, status.error

        execution = backend.prepare_tool(
            _ToolExecutionRequest(
                code="tools.math.add(1, 2)",
                cwd="subdir",
                environment={"SESSION_KEY": "session-value"},
                bindings=(_ToolBinding(name="math", path="tools/math.py"),),
            )
        )

        assert isinstance(execution, _ProcessExecution)
        payload = json.loads(execution._input_data.decode("utf-8"))
        assert payload["workspace"] == str(tmp_path.resolve())
        assert payload["cwd"] == str((tmp_path / "subdir").resolve())
        assert payload["tools_directory"] == str(tmp_path / ".workspace" / "tools")
        assert payload["tool_paths"] == {"math": "tools/math.py"}

    asyncio.run(scenario())


def test_prepare_tool_without_reconciled_runtime_fails_soft(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "example.py").write_text("VALUE = 1\n")
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    backend = _LocalBackendWorkspace(tmp_path, {}, view)

    async def scenario() -> None:
        execution = backend.prepare_tool(
            _ToolExecutionRequest(
                code="tools.example.VALUE",
                cwd="/workspace",
                environment={},
                bindings=(),
            )
        )

        assert isinstance(execution, _InlineExecution)
        assert not isinstance(execution, _ProcessExecution)

    asyncio.run(scenario())


def test_worker_environment_composes_backend_base_and_session_overlay(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "env_tool.py").write_text("VALUE = 1\n")
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    backend = _LocalBackendWorkspace(
        tmp_path,
        {"WS_KEY": "from-workspace"},
        view,
    )
    catalog = asyncio.run(_ToolCatalog.reconcile(view, backend.filesystem))

    async def scenario() -> None:
        status = await backend.reconcile_tool_runtime()
        assert status.available, status.error
        kernel = EnvironmentKernel(tmp_path, backend=backend, tool_catalog=catalog)
        try:
            await _exec(kernel, "export SESSION_KEY=session-value")

            workspace_value = _text(
                _output(
                    await _exec(
                        kernel, "tools run \"__import__('os').environ['WS_KEY']\""
                    )
                ),
                "stdout",
            )
            session_value = _text(
                _output(
                    await _exec(
                        kernel,
                        "tools run \"__import__('os').environ['SESSION_KEY']\"",
                    )
                ),
                "stdout",
            )
            virtual_env = _text(
                _output(
                    await _exec(
                        kernel,
                        "tools run \"__import__('os').environ['VIRTUAL_ENV']\"",
                    )
                ),
                "stdout",
            )
            path = _text(
                _output(
                    await _exec(
                        kernel, "tools run \"__import__('os').environ['PATH']\""
                    )
                ),
                "stdout",
            )

            assert workspace_value == "from-workspace\n"
            assert session_value == "session-value\n"
            assert virtual_env.strip().endswith(
                str(tmp_path / ".workspace" / ".tool-environment" / ".venv")
            )
            assert path.startswith(
                str(tmp_path / ".workspace" / ".tool-environment" / ".venv" / "bin")
                + ":"
            )
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_tool_worker_shares_backend_workspace_cwd_with_shell_and_files(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "marker.py").write_text(
        "from pathlib import Path\n"
        "def touch(path):\n"
        "    Path(path).write_text('made by tool\\n')\n"
    )
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    backend = _LocalBackendWorkspace(tmp_path, {}, view)
    catalog = asyncio.run(_ToolCatalog.reconcile(view, backend.filesystem))

    async def scenario() -> None:
        status = await backend.reconcile_tool_runtime()
        assert status.available, status.error
        kernel = EnvironmentKernel(tmp_path, backend=backend, tool_catalog=catalog)
        try:
            subdirectory = tmp_path / "shared-dir"
            subdirectory.mkdir()
            await _exec(kernel, "cd shared-dir")

            cwd = _text(
                _output(await _exec(kernel, "tools run \"__import__('os').getcwd()\"")),
                "stdout",
            )
            assert cwd.strip() == str(subdirectory.resolve())

            created = _output(
                await _exec(
                    kernel,
                    "tools run \"tools.marker.touch('relative-from-worker.txt')\"",
                )
            )
            assert created["status"] == "exited"

            assert (
                await backend.filesystem.read("shared-dir/relative-from-worker.txt")
                == b"made by tool\n"
            )
            shell = _text(
                _output(await _exec(kernel, "cat relative-from-worker.txt")),
                "stdout",
            )
            assert shell == "made by tool\n"
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_command_handlers_never_create_host_processes() -> None:
    package = importlib.import_module("cli_agent.runtime._environment.handlers")
    package_path = Path(package.__file__).parent
    for path in sorted(package_path.glob("*.py")):
        if path.name == "__init__.py":
            continue
        source = path.read_text(encoding="utf-8")
        assert "create_subprocess" not in source, path
        assert "asyncio.subprocess" not in source, path
        assert "import subprocess" not in source, path
        assert "os.environ" not in source, path
        assert "importlib" not in source, path


def test_tool_source_never_references_host_execution_details() -> None:
    from cli_agent.runtime._environment import sources as source_module

    source = Path(source_module.__file__).read_text(encoding="utf-8")

    assert "create_subprocess" not in source
    assert "os.environ" not in source
    assert "importlib" not in source
    assert "VIRTUAL_ENV" not in source
    assert "worker.py" not in source
    assert "venv" not in source


async def _exec(
    kernel: EnvironmentKernel,
    command: str,
    *,
    wait_ms: int = 8_000,
) -> ToolResult:
    return await kernel.dispatch(
        ToolCall(
            call_id=f"exec_{id(command)}",
            name="exec",
            arguments={"command": command, "wait_ms": wait_ms},
        )
    )


def _output(result: ToolResult) -> dict[str, object]:
    assert result.error is None
    assert isinstance(result.output, dict)
    return result.output


def _text(snapshot: dict[str, object], stream: str) -> str:
    chunks = snapshot["chunks"]
    assert isinstance(chunks, list)
    return "".join(
        str(chunk["text"])
        for chunk in chunks
        if isinstance(chunk, dict) and chunk.get("stream") == stream
    )
