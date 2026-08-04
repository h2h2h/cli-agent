import asyncio
import os
import shlex
from pathlib import Path

import pytest
from interaction_fakes import _ScriptedInteraction
from policy_fakes import _AskForWritesPolicy

from cli_agent.runtime import ToolCall, ToolResult
from cli_agent.runtime._capability.command_parser import parse_shell_ast
from cli_agent.runtime._capability.view import _CapabilityView
from cli_agent.runtime._environment import EnvironmentKernel
from cli_agent.runtime._environment.handlers.base import (
    _CommandContext,
    _ExecutionOutcome,
)
from cli_agent.runtime._environment.handlers.shell import _ShellHandler


def test_attach_exposes_lower_files_and_preserves_workspace_overrides(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    repertoire = tmp_path / "repertoire"
    workspace.mkdir()
    lower_tool = repertoire / "tools" / "calc.py"
    lower_skill = repertoire / "skills" / "review" / "SKILL.md"
    lower_tool.parent.mkdir(parents=True)
    lower_skill.parent.mkdir(parents=True)
    (repertoire / "library").mkdir()
    lower_tool.write_text("LOWER = 1\n", encoding="utf-8")
    lower_skill.write_text("# Review\n", encoding="utf-8")
    upper_tool = workspace / ".workspace" / "tools" / "local.py"
    upper_tool.parent.mkdir(parents=True)
    upper_tool.write_text("LOCAL = 1\n", encoding="utf-8")

    view = _CapabilityView.open(workspace, repertoire)

    visible_lower = workspace / ".workspace" / "tools" / "calc.py"
    visible_skill = workspace / ".workspace" / "skills" / "review" / "SKILL.md"
    assert visible_lower.is_symlink()
    assert visible_lower.read_text(encoding="utf-8") == "LOWER = 1\n"
    assert visible_skill.is_symlink()
    assert upper_tool.read_text(encoding="utf-8") == "LOCAL = 1\n"
    assert view.inspect("tools/calc.py").provenance == "repertoire"
    assert view.inspect("tools/local.py").provenance == "workspace"


def test_workspace_override_shadows_lower_across_reopen(tmp_path: Path) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "same.py"
    lower.write_text("lower-v1\n", encoding="utf-8")
    upper = workspace / ".workspace" / "tools" / "same.py"
    upper.parent.mkdir(parents=True)
    upper.write_text("workspace\n", encoding="utf-8")

    first = _CapabilityView.open(workspace, repertoire)
    lower.write_text("lower-v2\n", encoding="utf-8")
    second = _CapabilityView.open(workspace, repertoire)

    assert upper.is_file()
    assert not upper.is_symlink()
    assert upper.read_text(encoding="utf-8") == "workspace\n"
    assert first.inspect("tools/same.py").shadows_repertoire is True
    assert second.inspect("tools/same.py").shadows_repertoire is True


def test_invalid_workspace_override_remains_authoritative_and_is_reported(
    tmp_path: Path,
) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "invalid.py"
    lower.write_text("lower\n", encoding="utf-8")
    override = workspace / ".workspace" / "tools" / "invalid.py"
    override.mkdir(parents=True)

    view = _CapabilityView.open(workspace, repertoire)
    inspected = view.inspect("tools/invalid.py")

    assert override.is_dir()
    assert inspected.provenance == "workspace"
    assert inspected.shadows_repertoire is True
    assert inspected.valid is False
    assert inspected.validation_error is not None


def test_approved_output_redirection_copies_up_before_shell_spawn(
    tmp_path: Path,
) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "message.txt"
    lower.write_text("lower\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    visible = workspace / ".workspace" / "tools" / "message.txt"
    interaction = _ScriptedInteraction("allow_once")
    command = "echo workspace > .workspace/tools/message.txt"

    async def scenario() -> None:
        kernel = EnvironmentKernel(
            workspace,
            capability_view=view,
            policy=_AskForWritesPolicy(),
            user_interaction=interaction,
        )
        try:
            result = await _exec(kernel, command)
            assert _output(result)["status"] == "exited"
        finally:
            await kernel.close()

    asyncio.run(scenario())

    assert len(interaction.questions) == 1
    assert f"command: {command}" in interaction.questions[0].prompt
    assert lower.read_text(encoding="utf-8") == "lower\n"
    assert visible.is_file()
    assert not visible.is_symlink()
    assert visible.read_text(encoding="utf-8") == "workspace\n"
    assert view.inspect("tools/message.txt").shadows_repertoire is True


def test_denied_modification_does_not_copy_up(tmp_path: Path) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "preserved.txt"
    lower.write_text("lower\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    visible = workspace / ".workspace" / "tools" / "preserved.txt"

    async def scenario() -> None:
        kernel = EnvironmentKernel(
            workspace,
            capability_view=view,
            policy=_AskForWritesPolicy(),
            user_interaction=_ScriptedInteraction("deny"),
        )
        try:
            result = await _exec(
                kernel,
                "echo denied > .workspace/tools/preserved.txt",
            )
            assert result.error is not None
            assert result.error["code"] == "policy_denied"
        finally:
            await kernel.close()

    asyncio.run(scenario())

    assert visible.is_symlink()
    assert lower.read_text(encoding="utf-8") == "lower\n"


def test_rm_lower_link_removes_view_link_without_piercing_repertoire(
    tmp_path: Path,
) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "hidden.py"
    lower.write_text("lower\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    visible = workspace / ".workspace" / "tools" / "hidden.py"
    assert visible.is_symlink()

    _run_approved(workspace, view, "rm .workspace/tools/hidden.py")

    assert lower.is_file()
    assert not os.path.lexists(visible)
    assert view.inspect("tools/hidden.py").provenance is None

    reopened = _CapabilityView.open(workspace, repertoire)
    assert visible.is_symlink()
    assert visible.read_text(encoding="utf-8") == "lower\n"
    assert reopened.inspect("tools/hidden.py").provenance == "repertoire"


def test_rm_workspace_override_removes_entity_until_reopen(
    tmp_path: Path,
) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "restored.py"
    lower.write_text("lower\n", encoding="utf-8")
    upper = workspace / ".workspace" / "tools" / "restored.py"
    upper.parent.mkdir(parents=True)
    upper.write_text("workspace\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)

    _run_approved(workspace, view, "rm .workspace/tools/restored.py")

    assert not os.path.lexists(upper)
    assert view.inspect("tools/restored.py").provenance is None

    reopened = _CapabilityView.open(workspace, repertoire)
    assert upper.is_symlink()
    assert upper.read_text(encoding="utf-8") == "lower\n"
    assert reopened.inspect("tools/restored.py").provenance == "repertoire"


def test_rm_workspace_only_file_leaves_path_absent(tmp_path: Path) -> None:
    workspace, repertoire = _roots(tmp_path)
    local = workspace / ".workspace" / "tools" / "local.py"
    local.parent.mkdir(parents=True)
    local.write_text("workspace\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)

    _run_approved(workspace, view, "rm .workspace/tools/local.py")

    assert not local.exists()
    assert view.inspect("tools/local.py").provenance is None


def test_removing_capability_root_is_recreated_on_reopen(
    tmp_path: Path,
) -> None:
    workspace, repertoire = _roots(tmp_path)
    view = _CapabilityView.open(workspace, repertoire)
    tools = workspace / ".workspace" / "tools"

    _run_approved(workspace, view, "rmdir .workspace/tools")

    assert not tools.exists()

    _CapabilityView.open(workspace, repertoire)
    assert tools.is_dir()
    assert not tools.is_symlink()


def test_reopen_removes_generated_link_after_lower_file_is_removed(
    tmp_path: Path,
) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "removed.py"
    lower.write_text("lower\n", encoding="utf-8")
    _CapabilityView.open(workspace, repertoire)
    visible = workspace / ".workspace" / "tools" / "removed.py"
    assert visible.is_symlink()

    lower.unlink()
    reopened = _CapabilityView.open(workspace, repertoire)

    assert not os.path.lexists(visible)
    assert reopened.inspect("tools/removed.py").provenance is None


def test_copy_up_is_atomic_across_concurrent_sessions(tmp_path: Path) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "shared.txt"
    lower.write_text("lower\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    interaction = _ScriptedInteraction("allow_once")

    async def scenario() -> None:
        first = EnvironmentKernel(
            workspace,
            capability_view=view,
            policy=_AskForWritesPolicy(),
            user_interaction=interaction,
        )
        second = EnvironmentKernel(
            workspace,
            capability_view=view,
            policy=_AskForWritesPolicy(),
            user_interaction=interaction,
        )
        try:
            await asyncio.gather(
                _exec(
                    first,
                    "echo first > .workspace/tools/shared.txt",
                ),
                _exec(
                    second,
                    "echo second > .workspace/tools/shared.txt",
                ),
            )
        finally:
            await first.close()
            await second.close()

    asyncio.run(scenario())

    visible = workspace / ".workspace" / "tools" / "shared.txt"
    assert lower.read_text(encoding="utf-8") == "lower\n"
    assert not visible.is_symlink()
    assert visible.read_text(encoding="utf-8") in {"first\n", "second\n"}


def test_cancelled_shell_execution_does_not_copy_up(tmp_path: Path) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "cancelled.txt"
    lower.write_text("lower\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    visible = workspace / ".workspace" / "tools" / "cancelled.txt"

    async def scenario() -> None:
        execution = _ShellHandler(view).prepare(
            parse_shell_ast("echo x > .workspace/tools/cancelled.txt"),
            _CommandContext(
                workspace=workspace,
                cwd=workspace,
                environment={},
            ),
        )
        await execution.cancel()
        outcome = await execution.run(_DiscardOutput())

        assert outcome == _ExecutionOutcome.killed()

    asyncio.run(scenario())

    assert visible.is_symlink()
    assert lower.read_text(encoding="utf-8") == "lower\n"


def test_copy_up_rejects_symbolic_link_directory_traversal(
    tmp_path: Path,
) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower_directory = repertoire / "tools" / "nested"
    lower_directory.mkdir()
    lower = lower_directory / "protected.txt"
    lower.write_text("lower\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    visible_directory = workspace / ".workspace" / "tools" / "nested"
    for entry in tuple(visible_directory.iterdir()):
        entry.unlink()
    visible_directory.rmdir()
    visible_directory.symlink_to(lower_directory, target_is_directory=True)

    async def scenario() -> None:
        kernel = EnvironmentKernel(
            workspace,
            capability_view=view,
            policy=_AskForWritesPolicy(),
            user_interaction=_ScriptedInteraction("allow_once"),
        )
        try:
            result = await _exec(
                kernel,
                "echo changed > .workspace/tools/nested/protected.txt",
            )
            assert _output(result)["status"] == "failed"
        finally:
            await kernel.close()

    asyncio.run(scenario())

    assert lower.read_text(encoding="utf-8") == "lower\n"


def test_rejects_workspace_capability_symlink_that_forges_lower_origin(
    tmp_path: Path,
) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "real.py"
    lower.write_text("lower\n", encoding="utf-8")
    forged_target = tmp_path / "forged.py"
    forged_target.write_text("forged\n", encoding="utf-8")
    forged = workspace / ".workspace" / "tools" / "real.py"
    forged.parent.mkdir(parents=True)
    forged.symlink_to(forged_target)

    with pytest.raises(
        ValueError,
        match="matching Repertoire file",
    ):
        _CapabilityView.open(workspace, repertoire)

    assert forged.is_symlink()
    assert forged.read_text(encoding="utf-8") == "forged\n"


def test_rejects_repertoire_inside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(ValueError, match="outside the Workspace state"):
        _CapabilityView.open(
            workspace,
            workspace / ".workspace" / "repertoire",
        )


def test_allows_default_repertoire_beneath_workspace_ancestor(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "home"
    workspace.mkdir()
    repertoire = workspace / ".config" / "cli-agent" / "repertoire"

    view = _CapabilityView.open(workspace, repertoire)

    assert view.repertoire == repertoire
    assert (workspace / ".workspace" / "tools").is_dir()


def test_prepare_path_leaves_ordinary_files_untouched(tmp_path: Path) -> None:
    workspace, repertoire = _roots(tmp_path)
    outside = workspace / "notes.txt"
    outside.write_text("user\n", encoding="utf-8")
    lower = repertoire / "tools" / "calc.py"
    lower.write_text("LOWER = 1\n", encoding="utf-8")
    override = workspace / ".workspace" / "tools" / "local.py"
    override.parent.mkdir(parents=True)
    override.write_text("LOCAL = 1\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)

    view.prepare_path(outside)
    view.prepare_path(override)

    assert outside.read_text(encoding="utf-8") == "user\n"
    assert override.is_file()
    assert not override.is_symlink()
    assert override.read_text(encoding="utf-8") == "LOCAL = 1\n"
    assert lower.read_text(encoding="utf-8") == "LOWER = 1\n"
    assert view.inspect("tools/local.py").provenance == "workspace"


def test_prepare_path_copies_up_in_view_lower_link(tmp_path: Path) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "calc.py"
    lower.write_text("LOWER = 1\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    visible = workspace / ".workspace" / "tools" / "calc.py"
    assert visible.is_symlink()

    view.prepare_path(visible)

    assert not visible.is_symlink()
    assert visible.read_text(encoding="utf-8") == "LOWER = 1\n"
    assert lower.read_text(encoding="utf-8") == "LOWER = 1\n"
    assert view.inspect("tools/calc.py").provenance == "workspace"
    assert view.inspect("tools/calc.py").shadows_repertoire is True


def test_prepare_path_removes_whiteout_before_write(tmp_path: Path) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "hidden.py"
    lower.write_text("lower\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    visible = workspace / ".workspace" / "tools" / "hidden.py"
    whiteout = (
        workspace
        / ".workspace"
        / ".capability-view"
        / "whiteouts"
        / "tools"
        / "hidden.py"
    )
    whiteout.parent.mkdir(parents=True)
    whiteout.touch()
    visible.unlink()
    assert view.inspect("tools/hidden.py").provenance == "whiteout"

    view.prepare_path(visible)

    assert not os.path.lexists(visible)
    assert view.inspect("tools/hidden.py").provenance is None
    assert lower.is_file()
    assert lower.read_text(encoding="utf-8") == "lower\n"


def test_prepare_path_rejects_symlink_intermediate(tmp_path: Path) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower_directory = repertoire / "tools" / "nested"
    lower_directory.mkdir()
    lower = lower_directory / "protected.txt"
    lower.write_text("lower\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    visible_directory = workspace / ".workspace" / "tools" / "nested"
    for entry in tuple(visible_directory.iterdir()):
        entry.unlink()
    visible_directory.rmdir()
    visible_directory.symlink_to(lower_directory, target_is_directory=True)

    with pytest.raises(
        ValueError,
        match="must not traverse symbolic",
    ):
        view.prepare_path(visible_directory / "protected.txt")

    assert lower.read_text(encoding="utf-8") == "lower\n"


def test_prepare_path_allows_new_file_under_real_directory(
    tmp_path: Path,
) -> None:
    workspace, repertoire = _roots(tmp_path)
    view = _CapabilityView.open(workspace, repertoire)
    target = workspace / ".workspace" / "tools" / "generated" / "new.py"
    target.parent.mkdir(parents=True)

    view.prepare_path(target)

    assert not os.path.lexists(target)
    assert view.inspect("tools/generated/new.py").provenance is None


def test_prepare_path_rejects_invalid_lower_link(tmp_path: Path) -> None:
    workspace, repertoire = _roots(tmp_path)
    lower = repertoire / "tools" / "real.py"
    lower.write_text("lower\n", encoding="utf-8")
    forged_target = tmp_path / "forged.py"
    forged_target.write_text("forged\n", encoding="utf-8")
    view = _CapabilityView.open(workspace, repertoire)
    forged = workspace / ".workspace" / "tools" / "real.py"
    forged.unlink()
    forged.symlink_to(forged_target)

    with pytest.raises(ValueError, match="invalid Workspace capability symbolic"):
        view.prepare_path(forged)

    assert lower.read_text(encoding="utf-8") == "lower\n"


def test_shell_mutator_heuristics_are_removed_from_view() -> None:
    import cli_agent.runtime._capability.command_parser as parser_module
    import cli_agent.runtime._capability.view as view_module

    for symbol in (
        "_DIRECT_MUTATORS",
        "_sed_is_in_place",
        "_operands",
        "_DeleteSnapshot",
        "_delete_paths",
        "_snapshot_deletes",
        "_reconcile_deletes",
        "_create_whiteout",
    ):
        assert not hasattr(view_module, symbol)
        assert not hasattr(parser_module, symbol)


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    workspace = tmp_path / "workspace"
    repertoire = tmp_path / "repertoire"
    workspace.mkdir()
    for name in ("tools", "skills", "library"):
        (repertoire / name).mkdir(parents=True)
    return workspace, repertoire


def _run_approved(
    workspace: Path,
    view: _CapabilityView,
    command: str,
) -> None:
    async def scenario() -> None:
        kernel = EnvironmentKernel(
            workspace,
            capability_view=view,
            policy=_AskForWritesPolicy(),
            user_interaction=_ScriptedInteraction("allow_once"),
        )
        try:
            assert _output(await _exec(kernel, command))["status"] == "exited"
        finally:
            await kernel.close()

    asyncio.run(scenario())


async def _exec(kernel: EnvironmentKernel, command: str) -> ToolResult:
    return await kernel.dispatch(
        ToolCall(
            call_id=f"exec-{shlex.quote(command)}",
            name="exec",
            arguments={"command": command},
        )
    )


def _output(result: ToolResult) -> dict[str, object]:
    assert result.error is None
    assert isinstance(result.output, dict)
    return result.output


class _DiscardOutput:
    async def write(self, stream: str, data: bytes) -> None:
        del stream, data
