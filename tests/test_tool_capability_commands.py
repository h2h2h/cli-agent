import asyncio
import shlex
import subprocess
import sys
from pathlib import Path

import pytest
from policy_fakes import _AllowAllPolicy, _DenyExecutablePolicy

import cli_agent.runtime._capability.tools.environment as tool_environment_module
from cli_agent.runtime import (
    PolicyAction,
    RuntimeDiagnostic,
    ToolCall,
    ToolResult,
)
from cli_agent.runtime._backend.local import (
    _LocalBackendWorkspace,
    _LocalCapabilityView,
)
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime._capability.tools.environment import _ToolEnvironment
from cli_agent.runtime._capability.tools.facts import ToolCommand
from cli_agent.runtime._capability.tools.grammar import parse_tool_command
from cli_agent.runtime._capability.workspace import _prepare_workspace
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.policy import PolicyEvaluation
from cli_agent.runtime._system_message import assemble_system_message


def test_catalog_generates_index_and_reports_actual_provenance(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    lower = repertoire / "tools" / "lower.py"
    lower.write_text(
        '"""Lower arithmetic Tool."""\n\ndef add(a, b):\n    return a + b\n'
    )
    lower_index = repertoire / "tools" / "index.md"
    lower_index.write_text("user-owned lower index\n")

    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    local = Path(view.root) / "tools" / "local.py"
    local.write_text('"""Workspace Tool."""\nVALUE = 7\n')
    invalid = Path(view.root) / "tools" / "broken.py"
    invalid.write_text("def broken(:\n")
    keyword_name = Path(view.root) / "tools" / "class.py"
    keyword_name.write_text("VALUE = 1\n")

    catalog = asyncio.run(_ToolCatalog.reconcile(view))

    assert catalog.get("lower").provenance == "repertoire"  # type: ignore[union-attr]
    assert catalog.get("local").provenance == "workspace"  # type: ignore[union-attr]
    assert catalog.get("broken").valid is False  # type: ignore[union-attr]
    assert catalog.get("class").valid is False  # type: ignore[union-attr]
    index = Path(view.root) / "tools" / "index.md"
    assert index.is_file()
    assert not index.is_symlink()
    assert "lower | valid | repertoire" in index.read_text()
    assert "broken | invalid: Python syntax error" in index.read_text()
    assert lower_index.read_text() == "user-owned lower index\n"

    text, found = catalog.render_info("lower")
    assert found is True
    assert "Provenance: repertoire" in text
    assert "Lower arithmetic Tool." in text


def test_catalog_uses_tool_declarations_and_regular_tool_default(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "defaulted.py").write_text("VALUE = 1\n")
    (repertoire / "tools" / "serial.py").write_text(
        "PARALLEL_SAFE = False\nVALUE = 2\n"
    )
    (repertoire / "tools" / "typed.py").write_text(
        "PARALLEL_SAFE: bool = True\nVALUE = 3\n"
    )

    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    catalog = asyncio.run(_ToolCatalog.reconcile(view))

    assert catalog.get("defaulted").parallel_safe is True  # type: ignore[union-attr]
    assert catalog.get("serial").parallel_safe is False  # type: ignore[union-attr]
    assert catalog.get("typed").parallel_safe is True  # type: ignore[union-attr]
    index = (Path(view.root) / "tools" / "index.md").read_text()
    assert "| defaulted | valid | repertoire | no | yes |" in index
    info, found = catalog.render_info("serial")
    assert found is True
    assert "Parallel Safe: no" in info


def test_catalog_falls_back_and_reports_invalid_parallel_metadata(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "invalid.py").write_text(
        "PARALLEL_SAFE = 'yes'\nVALUE = 1\n"
    )
    (repertoire / "tools" / "duplicate.py").write_text(
        "PARALLEL_SAFE = True\nPARALLEL_SAFE = False\nVALUE = 2\n"
    )
    (repertoire / "tools" / "broken.py").write_text("def broken(:\n")

    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    diagnostics: list[RuntimeDiagnostic] = []
    catalog = asyncio.run(_ToolCatalog.reconcile(view, diagnostics.append))

    assert catalog.get("invalid").valid is True  # type: ignore[union-attr]
    assert catalog.get("invalid").parallel_safe is True  # type: ignore[union-attr]
    assert catalog.get("duplicate").parallel_safe is True  # type: ignore[union-attr]
    assert catalog.get("broken").valid is False  # type: ignore[union-attr]
    assert [diagnostic.kind for diagnostic in diagnostics] == [
        "tools.parallel_safe_parse_failed",
        "tools.parallel_safe_parse_failed",
        "tools.parallel_safe_parse_failed",
    ]
    assert {diagnostic.detail["tool"] for diagnostic in diagnostics} == {
        "broken",
        "duplicate",
        "invalid",
    }


def test_workspace_tool_override_controls_parallel_metadata(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "shared.py").write_text(
        "PARALLEL_SAFE = False\nVALUE = 1\n"
    )
    _prepare_workspace(tmp_path)
    override = tmp_path / ".workspace" / "tools" / "shared.py"
    override.parent.mkdir(parents=True, exist_ok=True)
    override.write_text("PARALLEL_SAFE = True\nVALUE = 2\n")

    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    catalog = asyncio.run(_ToolCatalog.reconcile(view))
    entry = catalog.get("shared")

    assert entry is not None
    assert entry.provenance == "workspace"
    assert entry.shadows_repertoire is True
    assert entry.parallel_safe is True


def test_system_message_embeds_only_compact_tools_catalog(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    lower = repertoire / "tools" / "lower.py"
    lower.write_text(
        '"""Lower arithmetic Tool."""\n\ndef add(a, b):\n    return a + b\n'
    )

    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    local = Path(view.root) / "tools" / "local.py"
    local.write_text('"""Workspace Tool."""\nVALUE = 7\n')
    broken = Path(view.root) / "tools" / "broken.py"
    broken.write_text("def broken(:\n")

    catalog = asyncio.run(_ToolCatalog.reconcile(view))
    message = assemble_system_message(tmp_path, None, tool_catalog=catalog)
    body = "\n".join(block.text for block in message.content)

    assert "Tools" in body
    assert "| lower | valid | yes | Lower arithmetic Tool. |" in body
    assert "| local | valid | yes | Workspace Tool. |" in body
    assert "| broken | invalid" in body
    assert "tools info <name>" in body
    assert "def add(a, b)" not in body
    assert "repertoire" not in body
    assert "Shadows Repertoire" not in body


def test_system_message_tools_section_omitted_without_catalog(
    tmp_path: Path,
) -> None:
    message = assemble_system_message(tmp_path, None)
    body = "\n".join(block.text for block in message.content)

    assert "compact Tool catalog lists" not in body
    assert "No Tools are currently discovered." not in body


def test_every_reserved_tool_form_evaluates_through_the_same_policy_hook(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "echo.py").write_text("def value():\n    return 'ok'\n")
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    catalog = asyncio.run(_ToolCatalog.reconcile(view))

    async def scenario() -> None:
        policy = _AllowAllPolicy()
        commands = (
            "tools list",
            "tools info echo",
            'tools run "tools.echo.value()"',
            "tools run <<'PY'\ntools.echo.value()\nPY",
            "tools list | cat",
        )
        operations = ("list", "inspect", "run", "run", "invalid")
        for raw, operation in zip(commands, operations, strict=True):
            command = parse_shell_ast(raw)
            facts = parse_tool_command(command, catalog)
            assert isinstance(facts, ToolCommand)
            assert facts.operation == operation
            evaluation = await policy.evaluate(command)
            assert evaluation.action is PolicyAction.ALLOW
            assert evaluation.rule_id == "test.allow"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("raw", "reserved", "operation"),
    (
        ('"tools" list', True, "list"),
        ('  tools run "print(1)"  ', True, "run"),
        ("tools list > ignored.txt", True, "invalid"),
        ("tools list | cat", True, "invalid"),
        ("tools list &", True, "invalid"),
        ("tools list; echo bypass", True, "invalid"),
        ('tools run "print(1 > 0)"', True, "run"),
        ('tools run "unterminated', False, None),
        ("to\\ols list", True, "list"),
        ("A=1 tools list", False, None),
        ("$COMMAND list", False, None),
        ("env tools list", False, None),
        ("/usr/local/bin/tools list", False, None),
        ("./tools list", False, None),
        ("toolsmith list", False, None),
    ),
)
def test_reserved_tool_grammar_cannot_fall_through_to_shell(
    tmp_path: Path,
    raw: str,
    reserved: bool,
    operation: str | None,
) -> None:
    repertoire = _repertoire(tmp_path)
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    catalog = asyncio.run(_ToolCatalog.reconcile(view))

    command = parse_shell_ast(raw)
    facts = parse_tool_command(command, catalog)

    assert (facts is not None) is reserved
    if facts is not None:
        assert facts.operation == operation


@pytest.mark.parametrize(
    "raw",
    (
        'tools info "$NAME"',
        "tools run code",
        "tools run <<'CODE'\nprint(1)\nCODE",
        "tools run <<-'PY'\n\tprint(1)\n\tPY",
        "tools run <<'PY' > ignored.txt\nprint(1)\nPY",
    ),
)
def test_tool_grammar_rejects_unsupported_ast_shapes(
    tmp_path: Path,
    raw: str,
) -> None:
    repertoire = _repertoire(tmp_path)
    _prepare_workspace(tmp_path)
    catalog = asyncio.run(
        _ToolCatalog.reconcile(
            _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        )
    )

    facts = parse_tool_command(parse_shell_ast(raw), catalog)

    assert facts is not None
    assert facts.operation == "invalid"


def test_tool_run_extracts_quoted_and_heredoc_payloads_from_ast(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    _prepare_workspace(tmp_path)
    catalog = asyncio.run(
        _ToolCatalog.reconcile(
            _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
        )
    )

    quoted = parse_tool_command(
        parse_shell_ast("tools run \"print('hello')\""),
        catalog,
    )
    heredoc = parse_tool_command(
        parse_shell_ast("tools run <<'PY'\nprint('hello')\nPY"),
        catalog,
    )

    assert quoted is not None
    assert quoted.operation == "run"
    assert quoted.code == "print('hello')"
    assert heredoc is not None
    assert heredoc.operation == "run"
    assert heredoc.code == "print('hello')"


def test_host_can_deny_tool_executable_through_the_policy_hook(
    tmp_path: Path,
) -> None:
    command = parse_shell_ast("tools list")
    policy = _DenyExecutablePolicy(
        frozenset({"tools"}),
        reason="tools is denied by policy",
    )

    async def scenario() -> None:
        evaluation = await policy.evaluate(command)
        assert evaluation.action is PolicyAction.DENY
        assert evaluation.rule_id == "test.deny-executable"

    asyncio.run(scenario())


def test_tools_list_info_and_reserved_invalid_syntax_use_tool_handler(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "hello.py").write_text(
        '"""Say hello."""\n\ndef say():\n    return "hello"\n'
    )

    async def scenario() -> None:
        kernel = await _kernel(tmp_path, repertoire)
        try:
            registered = kernel._router._custom_registry.resolve(
                parse_shell_ast("tools list")
            )
            assert registered is not None
            assert registered.name == "tools"
            assert not hasattr(kernel._router, "_tool_command")

            listed = _output(await _exec(kernel, "tools list"))
            assert listed["status"] == "exited"
            assert "hello | valid | repertoire" in _text(listed, "stdout")

            info = _output(await _exec(kernel, "tools info hello"))
            assert info["status"] == "exited"
            assert "Say hello." in _text(info, "stdout")

            invalid = _output(await _exec(kernel, "tools list | cat"))
            assert invalid["status"] == "failed"
            assert "Usage: tools" in _text(invalid, "stderr")
            state = kernel._executions[str(invalid["exec_id"])]
            assert state.route.command is registered
            assert state.route.command.name == "tools"
            assert state.route.parallel_safe is False
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_custom_and_shell_commands_share_one_execution_lifecycle(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "hello.py").write_text(
        '"""Greeting Tool."""\nVALUE = "hello"\n'
    )
    child = tmp_path / "child"
    child.mkdir()

    async def scenario() -> None:
        kernel = await _kernel(tmp_path, repertoire)
        try:
            commands = (
                ("cd child", "cd", False),
                ("export UNIFIED=yes", "export", False),
                ("tools list", "tools", True),
                ("export", "export", False),
                ("pwd", None, False),
            )
            snapshots: list[dict[str, object]] = []
            for command, route_name, parallel_safe in commands:
                snapshot = _output(await _exec(kernel, command))
                state = kernel._executions[str(snapshot["exec_id"])]

                assert state.route.command.name == route_name
                assert state.route.parallel_safe is parallel_safe
                assert {
                    "exec_id",
                    "status",
                    "exit_code",
                    "chunks",
                    "next_cursor",
                    "is_terminal",
                    "truncated",
                    "available_from",
                } <= snapshot.keys()
                assert snapshot["status"] == "exited"
                snapshots.append(snapshot)

            assert "hello | valid | repertoire" in _text(snapshots[2], "stdout")
            assert "UNIFIED=yes" in _text(snapshots[3], "stdout")
            assert _text(snapshots[4], "stdout").strip() == str(child)
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_reserved_tools_without_catalog_do_not_fall_back_to_shell(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(tmp_path)
        try:
            result = _output(await _exec(kernel, "tools list"))
            assert result["status"] == "failed"
            assert "Tool catalog is unavailable" in _text(result, "stderr")
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_tools_run_supports_quoted_and_heredoc_python(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "math_tool.py").write_text(
        '"""Arithmetic."""\n\ndef add(a, b):\n    return a + b\n'
    )

    async def scenario() -> None:
        kernel = await _kernel(tmp_path, repertoire)
        try:
            quoted = _output(
                await _exec(kernel, 'tools run "tools.math_tool.add(2, 3)"')
            )
            assert quoted["status"] == "exited"
            assert _text(quoted, "stdout") == "5\n"

            heredoc = _output(
                await _exec(
                    kernel,
                    (
                        "tools run <<'PY'\n"
                        "values = [tools.math_tool.add(1, 2), 4]\n"
                        "json.dumps(values)\n"
                        "PY"
                    ),
                )
            )
            assert heredoc["status"] == "exited"
            assert _text(heredoc, "stdout") == "[3, 4]\n"
            assert _text(heredoc, "stderr") == ""
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_fresh_tool_workers_isolate_module_state_and_use_private_python(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "counter.py").write_text(
        "count = 0\n"
        "def increment():\n"
        "    global count\n"
        "    count += 1\n"
        "    return count\n"
    )

    async def scenario() -> None:
        kernel = await _kernel(tmp_path, repertoire)
        try:
            first = _output(
                await _exec(kernel, 'tools run "tools.counter.increment()"')
            )
            second = _output(
                await _exec(kernel, 'tools run "tools.counter.increment()"')
            )
            prefix = _output(
                await _exec(
                    kernel,
                    "tools run \"__import__('sys').prefix\"",
                )
            )
            assert _text(first, "stdout") == "1\n"
            assert _text(second, "stdout") == "1\n"
            assert str(
                tmp_path / ".workspace" / ".tool-environment" / ".venv"
            ) in _text(prefix, "stdout")
            assert _text(prefix, "stdout").strip() != sys.prefix
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_tool_grammar_facts_stay_out_of_policy(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "valid_tool.py").write_text("VALUE = 1\n")
    (repertoire / "tools" / "invalid_tool.py").write_text("VALUE =\n")
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    catalog = asyncio.run(_ToolCatalog.reconcile(view))

    class RecordingPolicy:
        def __init__(self) -> None:
            self.commands = []

        async def evaluate(self, command):
            self.commands.append(command)
            return PolicyEvaluation(
                action=PolicyAction.ALLOW,
                rule_id="test.allow",
            )

    async def scenario() -> None:
        policy = RecordingPolicy()
        kernel = await _kernel(tmp_path, repertoire, policy=policy)
        try:
            await _exec(kernel, 'tools run "tools.valid_tool.VALUE"')
            await _exec(kernel, "tools info invalid_tool")

            assert all(not hasattr(command, "tool") for command in policy.commands)
            run = parse_tool_command(policy.commands[0], catalog)
            assert run is not None
            assert run.operation == "run"
            assert run.references[0].name == "valid_tool"
            assert run.references[0].provenance == "repertoire"
            assert run.references[0].valid is True

            inspected = parse_tool_command(
                policy.commands[1],
                catalog,
            )
            assert inspected is not None
            assert inspected.operation == "inspect"
            assert inspected.references[0].valid is False
            assert "syntax error" in inspected.references[0].validation_error
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_unavailable_tool_environment_fails_run_without_host_fallback(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "example.py").write_text("VALUE = 1\n")
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    catalog = asyncio.run(_ToolCatalog.reconcile(view))
    unavailable = _ToolEnvironment(
        root=Path(view.root) / ".tool-environment",
        python=None,
        error="Tool environment is unavailable: sync failed",
    )

    async def scenario() -> None:
        kernel = EnvironmentKernel(
            tmp_path,
            backend=_LocalBackendWorkspace(tmp_path, {}, view),
            tool_catalog=catalog,
            tool_environment=unavailable,
        )
        try:
            listed = _output(await _exec(kernel, "tools list"))
            assert listed["status"] == "exited"
            assert "example | valid | repertoire" in _text(listed, "stdout")
            info = _output(await _exec(kernel, "tools info example"))
            assert info["status"] == "exited"

            result = _output(await _exec(kernel, 'tools run "tools.example.VALUE"'))
            assert result["status"] == "failed"
            assert "sync failed" in _text(result, "stderr")
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_tool_environment_syncs_user_requirements_plus_runtime_base(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repertoire = _repertoire(tmp_path)
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    requirements = Path(view.root) / "tools" / "requirements.txt"

    calls: list[tuple[Path, Path, Path]] = []

    async def record_sync(
        *,
        python: Path,
        requirements: Path,
        working_directory: Path,
    ) -> None:
        calls.append((python, requirements, working_directory))

    monkeypatch.setattr(
        tool_environment_module,
        "_sync_requirements",
        record_sync,
    )

    async def scenario() -> None:
        initial = await _ToolEnvironment.reconcile(view)
        assert initial.available
        assert len(calls) == 1
        assert calls[0][1].read_text() == "mcp\n"

        requirements.write_text("example-package==1.2.3\n")
        changed = await _ToolEnvironment.reconcile(view)
        unchanged = await _ToolEnvironment.reconcile(view)

        assert changed.available and unchanged.available
        assert len(calls) == 2
        assert calls[0][0] == changed.python
        assert calls[1][1].read_text() == "example-package==1.2.3\nmcp\n"
        assert calls[1][2] == Path(view.root) / "tools"
        assert changed.root == tmp_path / ".workspace" / ".tool-environment"

        other_workspace = tmp_path / "other"
        other_workspace.mkdir()
        other_repertoire = _repertoire(other_workspace)
        _prepare_workspace(other_workspace)
        other_view = _LocalCapabilityView.materialize(
            other_workspace / ".workspace", other_repertoire
        )
        other = await _ToolEnvironment.reconcile(other_view)
        assert other.root != changed.root
        assert other.python != changed.python

    asyncio.run(scenario())


def test_effective_requirements_do_not_duplicate_user_declared_base(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repertoire = _repertoire(tmp_path)
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    requirements = Path(view.root) / "tools" / "requirements.txt"
    requirements.write_text("requests\nmcp\n")

    async def record_sync(
        *,
        python: Path,
        requirements: Path,
        working_directory: Path,
    ) -> None:
        del python, requirements, working_directory

    monkeypatch.setattr(
        tool_environment_module,
        "_sync_requirements",
        record_sync,
    )

    async def scenario() -> None:
        await _ToolEnvironment.reconcile(view)
        effective = (
            Path(view.root) / ".tool-environment" / "effective-requirements.txt"
        ).read_text(encoding="utf-8")
        assert effective == "requests\nmcp\n"

    asyncio.run(scenario())


@pytest.mark.live_sync
def test_mcp_is_importable_in_the_worker_venv(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)

    async def scenario() -> None:
        environment = await _ToolEnvironment.reconcile(view)
        assert environment.available, environment.error
        assert environment.python is not None
        result = subprocess.run(
            [
                str(environment.python),
                "-c",
                "import mcp; import mcp_types",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

    asyncio.run(scenario())


def test_dependency_sync_failure_is_fail_soft_for_catalog_operations(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "visible.py").write_text("VALUE = 1\n")
    _prepare_workspace(tmp_path)
    view = _LocalCapabilityView.materialize(tmp_path / ".workspace", repertoire)
    requirements = Path(view.root) / "tools" / "requirements.txt"
    requirements.write_text("unavailable-package==0\n")

    async def fail_sync(
        *,
        python: Path,
        requirements: Path,
        working_directory: Path,
    ) -> None:
        del python, requirements, working_directory
        raise RuntimeError("package manager failed")

    monkeypatch.setattr(
        tool_environment_module,
        "_sync_requirements",
        fail_sync,
    )

    async def scenario() -> None:
        environment = await _ToolEnvironment.reconcile(view)
        catalog = await _ToolCatalog.reconcile(view)
        assert environment.available is False
        assert "package manager failed" in environment.error

        kernel = EnvironmentKernel(
            tmp_path,
            backend=_LocalBackendWorkspace(tmp_path, {}, view),
            tool_catalog=catalog,
            tool_environment=environment,
        )
        try:
            listed = _output(await _exec(kernel, "tools list"))
            info = _output(await _exec(kernel, "tools info visible"))
            run = _output(await _exec(kernel, 'tools run "tools.visible.VALUE"'))
            assert listed["status"] == "exited"
            assert info["status"] == "exited"
            assert run["status"] == "failed"
            assert "package manager failed" in _text(run, "stderr")
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_tool_waits_behind_serial_shell_barrier(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "marker.py").write_text(
        "from pathlib import Path\ndef touch(path):\n    Path(path).touch()\n"
    )
    shell_started = tmp_path / "shell-started"
    shell_release = tmp_path / "shell-release"
    tool_finished = tmp_path / "tool-finished"

    async def scenario() -> None:
        kernel = await _kernel(tmp_path, repertoire)
        try:
            shell = _output(
                await _exec(
                    kernel,
                    _blocking_command(shell_started, shell_release),
                    wait_ms=0,
                )
            )
            await _wait_for_path(shell_started)
            tool = _output(
                await _exec(
                    kernel,
                    (f'tools run "tools.marker.touch({str(tool_finished)!r})"'),
                    wait_ms=0,
                )
            )
            assert shell["status"] == "running"
            assert tool["status"] == "queued"
            assert not tool_finished.exists()

            shell_release.touch(exist_ok=True)
            assert (await _read_until_terminal(kernel, str(tool["exec_id"])))[
                "status"
            ] == "exited"
            assert tool_finished.exists()
        finally:
            shell_release.touch(exist_ok=True)
            await kernel.close()

    asyncio.run(scenario())


def test_tool_shares_parallel_capacity_with_shell(tmp_path: Path) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "marker.py").write_text(
        "from pathlib import Path\ndef touch(path):\n    Path(path).touch()\n"
    )
    shell_started = tmp_path / "parallel-shell-started"
    shell_release = tmp_path / "parallel-shell-release"
    tool_finished = tmp_path / "parallel-tool-finished"

    async def scenario() -> None:
        kernel = await _kernel(
            tmp_path,
            repertoire,
            parallel_commands=frozenset({Path(sys.executable).name}),
            parallel_limit=1,
        )
        try:
            shell = _output(
                await _exec(
                    kernel,
                    _blocking_command(shell_started, shell_release),
                    wait_ms=0,
                )
            )
            await _wait_for_path(shell_started)
            tool = _output(
                await _exec(
                    kernel,
                    f'tools run "tools.marker.touch({str(tool_finished)!r})"',
                    wait_ms=0,
                )
            )

            assert shell["status"] == "running"
            assert tool["status"] == "queued"
            assert not tool_finished.exists()

            shell_release.touch(exist_ok=True)
            assert (await _read_until_terminal(kernel, str(tool["exec_id"])))[
                "status"
            ] == "exited"
            assert tool_finished.exists()
        finally:
            shell_release.touch(exist_ok=True)
            await kernel.close()

    asyncio.run(scenario())


def test_catalog_tool_metadata_controls_parallel_scheduling(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "allowed.py").write_text("VALUE = 1\n")
    (repertoire / "tools" / "other.py").write_text("VALUE = 2\n")

    async def scenario() -> None:
        kernel = await _kernel(tmp_path, repertoire)
        try:
            allowed = _output(await _exec(kernel, 'tools run "tools.allowed.VALUE"'))
            mixed = _output(
                await _exec(
                    kernel,
                    'tools run "tools.allowed.VALUE + tools.other.VALUE"',
                )
            )
            dynamic = _output(
                await _exec(kernel, "tools run \"getattr(tools, 'allowed').VALUE\"")
            )
            assert (
                kernel._executions[str(allowed["exec_id"])].route.parallel_safe is True
            )
            assert kernel._executions[str(mixed["exec_id"])].route.parallel_safe is True
            assert (
                kernel._executions[str(dynamic["exec_id"])].route.parallel_safe is False
            )
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_batch_dispatch_preserves_result_order_when_parallel_commands_finish_out_of_order(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)
    (repertoire / "tools" / "timed.py").write_text(
        "import time\n"
        "from pathlib import Path\n"
        "def finish(path, delay, value):\n"
        "    time.sleep(delay)\n"
        "    Path(path).write_text(str(time.time_ns()))\n"
        "    return value\n"
    )
    first_finished = tmp_path / "first-finished"
    second_finished = tmp_path / "second-finished"

    async def scenario() -> None:
        kernel = await _kernel(tmp_path, repertoire)
        try:
            calls = (
                ToolCall(
                    call_id="first",
                    name="exec",
                    arguments={
                        "command": (
                            "tools run "
                            f'"tools.timed.finish({str(first_finished)!r}, 0.2, '
                            "'first')\""
                        )
                    },
                ),
                ToolCall(
                    call_id="second",
                    name="exec",
                    arguments={
                        "command": (
                            "tools run "
                            f'"tools.timed.finish({str(second_finished)!r}, 0.01, '
                            "'second')\""
                        )
                    },
                ),
            )

            results = await kernel.dispatch_batch(calls)

            assert tuple(result.call_id for result in results) == ("first", "second")
            assert _text(_output(results[0]), "stdout") == "first\n"
            assert _text(_output(results[1]), "stdout") == "second\n"
            assert int(second_finished.read_text()) < int(first_finished.read_text())
        finally:
            await kernel.close()

    asyncio.run(scenario())


def test_kill_terminates_tool_worker_through_shared_execution_path(
    tmp_path: Path,
) -> None:
    repertoire = _repertoire(tmp_path)

    async def scenario() -> None:
        kernel = await _kernel(tmp_path, repertoire)
        try:
            running = _output(
                await _exec(
                    kernel,
                    "tools run \"__import__('time').sleep(30)\"",
                    wait_ms=0,
                )
            )
            killed = _output(
                await kernel.dispatch(
                    ToolCall(
                        call_id="kill_tool",
                        name="kill",
                        arguments={"exec_id": running["exec_id"]},
                    )
                )
            )
            assert killed["status"] == "killed"
            assert killed["is_terminal"] is True
        finally:
            await kernel.close()

    asyncio.run(scenario())


async def _kernel(
    workspace: Path,
    repertoire: Path,
    *,
    policy=None,
    parallel_commands: frozenset[str] = frozenset(),
    parallel_limit: int = 4,
) -> EnvironmentKernel:
    _prepare_workspace(workspace)
    view = _LocalCapabilityView.materialize(workspace / ".workspace", repertoire)
    catalog = await _ToolCatalog.reconcile(view)
    environment = await _ToolEnvironment.reconcile(view)
    assert environment.available, environment.error
    return EnvironmentKernel(
        workspace,
        backend=_LocalBackendWorkspace(workspace, {}, view),
        tool_catalog=catalog,
        tool_environment=environment,
        policy=policy,
        parallel_commands=parallel_commands,
        parallel_limit=parallel_limit,
    )


def _repertoire(workspace: Path) -> Path:
    repertoire = workspace.parent / f"{workspace.name}-repertoire"
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True, exist_ok=True)
    return repertoire


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


async def _read_until_terminal(
    kernel: EnvironmentKernel,
    exec_id: str,
) -> dict[str, object]:
    for index in range(100):
        snapshot = _output(
            await kernel.dispatch(
                ToolCall(
                    call_id=f"output_{index}",
                    name="output",
                    arguments={"exec_id": exec_id, "wait_ms": 100},
                )
            )
        )
        if snapshot["is_terminal"]:
            return snapshot
    raise AssertionError("execution did not reach a terminal state")


def _blocking_command(started: Path, release: Path) -> str:
    source = (
        "import time; from pathlib import Path; "
        f"started = Path({str(started)!r}); "
        f"release = Path({str(release)!r}); "
        "started.touch(); "
        "\nwhile not release.exists(): time.sleep(0.01)"
    )
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


async def _wait_for_path(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"path did not appear: {path}")
