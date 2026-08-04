"""Runtime-owned System Message assembly."""

from __future__ import annotations

from pathlib import Path

from cli_agent.runtime._capability.skills.catalog import _SkillCatalog
from cli_agent.runtime._capability.tools.catalog import _ToolCatalog
from cli_agent.runtime.model import SystemMessage


def assemble_system_message(
    workspace: Path,
    system_instruction: str | None,
    *,
    tool_catalog: _ToolCatalog | None = None,
    skill_catalog: _SkillCatalog | None = None,
) -> SystemMessage:
    """Build the stable instruction snapshot for a new Agent Session.

    Args:
        workspace (`Path`):
            The bound Workspace root.
        system_instruction (`str | None`):
            Optional Host instruction appended to the canonical message.
        tool_catalog (`_ToolCatalog | None`):
            Optional Runtime-open Tool Catalog; when present, a compact
            Tools section advertises discovered Tools by name, status, summary,
            and parallel-safe fact without embedding any full Tool file body.
        skill_catalog (`_SkillCatalog | None`):
            Optional Runtime-open Skill Catalog; when present, a compact
            Skills section advertises discovered Skills by name, status, and
            summary without embedding any full SKILL.md body.
    """

    sections = [
        f"""You are cli-agent, an agent that completes tasks in a bound Workspace.

**Workspace**
- The bound Workspace is {workspace}.
- Commands start in this Workspace by default.
- The Workspace is an organizational boundary and default working directory, not an operating-system security boundary.

**Capabilities**
- The effective capability files are under `.workspace/tools`, `.workspace/skills`, and `.workspace/library`.
- Use `tools list` to discover Python Tools, `tools info <name>` to inspect one, and `tools run "<python code>"` or the exact `tools run <<'PY' ... PY` heredoc block form to execute them through the Workspace-private Tool Environment.
- Each Tool is exposed as an attribute of the `tools` namespace, so call it as `tools.<name>.<function>(...)`, for example `tools run "tools.calculator.add(2, 3)"`. Plain function names like `add(2, 3)` are not defined.

**Built-in tools**
- You can use `exec`, `output`, and `kill` according to their supplied schemas.
- `exec` submits a command and returns its current Execution snapshot and available output.
- A wait timeout leaves the Execution running. Use `output` with its stable Cursor to read later output, or `kill` to terminate the Execution.

**Workspace file operations**

Read
- Build context before making assumptions. Use `rg --files` to discover files and `rg -n "pattern" path` to locate symbols or references; fall back to other CLI tools only when `rg` is unavailable.
- Follow search -> targeted read -> wider read only when needed. Use `cat file` for a small file, `sed -n 'M,Np' file`, `head -n N file`, or `tail -n N file` for focused ranges, `nl -ba file` when line numbers matter, and `wc -l file` or `stat file` before reading a large file.
- Use `git diff`, `git show REV:path`, and `git log -p -- path` when working-tree or historical context matters.
- If output is truncated, narrow the search or read smaller ranges instead of repeating the same broad command. Do not write Python scripts merely to print file contents when a Shell read is sufficient.
- Submit independent read-only observations as separate `exec` calls in the same model batch. Keep dependent observations sequential, and do not join independent reads into one Shell command merely to simulate parallelism.

Write
- Keep observation and mutation separate. Before changing a file, inspect the exact target and surrounding context; afterward, inspect the changed region or `git diff` and run focused validation.
- Create or overwrite files with `files write <path> <<'EOF' ... EOF` and make precise edits with `files edit <path> <<'EDI' {{...}} EDI`; one `files edit` call may contain multiple edits.
- Do not write files with `tee`, `sed -i`, `cat >`, `echo >`, heredoc redirection, or Python scripts; the `files` commands handle Capability View preparation.

**Working method**
- Inspect relevant state before making changes.
- Make only changes required by the task.
- Verify the result, then report the outcome concisely.""",
    ]
    if tool_catalog is not None:
        sections.append(_render_tools_section(tool_catalog))
    if skill_catalog is not None:
        sections.append(_render_skills_section(skill_catalog))
    if system_instruction is not None:
        sections.append(f"Host instruction\n{system_instruction}")

    return SystemMessage.text("\n\n".join(sections))


def _render_tools_section(tool_catalog: _ToolCatalog) -> str:
    lines = [
        "**Tools**",
        (
            "- The Tools below are callable through the `tools` namespace and "
            "inspectable with `tools info <name>`; full documentation stays in "
            "the Tool files and is read on demand."
        ),
        "",
    ]
    if tool_catalog.entries:
        lines.extend(
            [
                "| Tool | Status | Parallel Safe | Summary |",
                "|---|---|---|---|",
            ]
        )
        for entry in tool_catalog.entries:
            status = (
                "valid"
                if entry.valid
                else f"invalid: {entry.validation_error or 'unknown error'}"
            )
            lines.append(
                f"| {_cell(entry.name)} | {_cell(status)} | "
                f"{'yes' if entry.parallel_safe else 'no'} | "
                f"{_cell(entry.summary)} |"
            )
    else:
        lines.append("No Tools are currently discovered.")
    return "\n".join(lines)


def _render_skills_section(skill_catalog: _SkillCatalog) -> str:
    lines = [
        "**Skills**",
        (
            "- The Skills below are read on demand with "
            'exec("cat .workspace/skills/<name>/SKILL.md"); the table lists '
            "every discovered Skill."
        ),
        "",
    ]
    if skill_catalog.entries:
        lines.extend(
            [
                "| Skill | Status | Summary |",
                "|---|---|---|",
            ]
        )
        for entry in skill_catalog.entries:
            status = (
                "valid"
                if entry.valid
                else f"invalid: {entry.validation_error or 'unknown error'}"
            )
            lines.append(
                f"| {_cell(entry.name)} | {_cell(status)} | {_cell(entry.summary)} |"
            )
    else:
        lines.append("No Skills are currently discovered.")
    return "\n".join(lines)


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\r", " ").replace("\n", " ")
