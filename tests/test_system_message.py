"""Contract tests for the static Workspace exploration guidance."""

from pathlib import Path

from cli_agent.runtime._system_message import assemble_system_message


def test_guidance_defines_a_search_then_targeted_read_workflow(
    tmp_path: Path,
) -> None:
    guidance = _guidance_text(tmp_path)

    assert "search -> targeted read -> wider read only when needed" in guidance
    assert guidance.index("`rg --files`") < guidance.index("`cat file`")
    assert '`rg -n "pattern" path`' in guidance
    assert "`sed -n 'M,Np' file`" in guidance
    assert "`head -n N file`" in guidance
    assert "`tail -n N file`" in guidance
    assert "`nl -ba file`" in guidance
    assert "`wc -l file` or `stat file`" in guidance
    assert "`git diff`, `git show REV:path`, and `git log -p -- path`" in guidance


def test_guidance_explains_truncation_and_parallel_read_boundaries(
    tmp_path: Path,
) -> None:
    guidance = _guidance_text(tmp_path)

    assert "If output is truncated" in guidance
    assert "narrow the search or read smaller ranges" in guidance
    assert "separate `exec` calls in the same model batch" in guidance
    assert "dependent observations sequential" in guidance
    assert "do not join independent reads into one Shell command" in guidance


def test_guidance_references_only_actual_runtime_capabilities(
    tmp_path: Path,
) -> None:
    guidance = _guidance_text(tmp_path)

    assert "Do not write Python scripts merely to print file contents" in guidance
    assert "Keep observation and mutation separate" in guidance
    assert "inspect the changed region or `git diff`" in guidance
    assert "run focused validation" in guidance
    assert "apply_patch" not in guidance
    assert "workdir" not in guidance
    assert "Read Tool" not in guidance
    assert "sandbox" not in guidance
    assert "side-effect" not in guidance.lower()


def test_guidance_promotes_files_commands_for_mutations(tmp_path: Path) -> None:
    guidance = _guidance_text(tmp_path)

    assert "`files write <path> <<'EOF' ... EOF`" in guidance
    assert "`files edit <path> <<'EDI' {...} EDI`" in guidance
    assert "one `files edit` call may contain multiple edits" in guidance
    for banned in ("`tee`", "`sed -i`", "`cat >`", "`echo >`", "Python scripts"):
        assert banned in guidance
    assert "Capability View preparation" in guidance


def test_guidance_splits_into_read_and_write_parts(tmp_path: Path) -> None:
    guidance = _guidance_text(tmp_path)
    read_part, write_part = guidance.split("\n\nWrite\n", maxsplit=1)

    assert read_part.lstrip().startswith("Read\n")
    assert "`rg --files`" in read_part
    assert "`cat file`" in read_part
    assert "files write" not in read_part
    assert "`files write <path> <<'EOF' ... EOF`" in write_part
    assert "Keep observation and mutation separate" in write_part


def _system_message_text(workspace: Path) -> str:
    message = assemble_system_message(workspace, None)
    return "\n".join(block.text for block in message.content)


def _guidance_text(workspace: Path) -> str:
    body = _system_message_text(workspace)
    _, rest = body.split("**Workspace file operations**\n", maxsplit=1)
    guidance, _ = rest.split("\n\n**Working method**", maxsplit=1)
    return guidance
