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
            Tools section advertises discovered Tools by name, status, and
            summary without embedding any full Tool file body.
        skill_catalog (`_SkillCatalog | None`):
            Optional Runtime-open Skill Catalog; when present, a compact
            Skills section advertises discovered Skills by name, status, and
            summary without embedding any full SKILL.md body.
    """

    sections = [
        f"""You are cli-agent, an agent that completes tasks in a bound Workspace.

Workspace
- The bound Workspace is {workspace}.
- Commands start in this Workspace by default.
- The Workspace is an organizational boundary and default working directory, not an operating-system security boundary.

Capabilities
- The effective capability files are under `.workspace/tools`, `.workspace/skills`, and `.workspace/library`.
- These paths merge a user-maintained Repertoire with Workspace-local files. Workspace files take precedence.
- Commands that the Runtime recognizes as modifying files may require Host approval before they start.
- Use `tools list` to discover Python Tools, `tools info <name>` to inspect one, and `tools run "<python code>"` or the exact `tools run PY<< ... PY` block form to execute them through the Workspace-private Tool Environment.

Built-in tools
- You can use `exec`, `output`, and `kill` according to their supplied schemas.
- `exec` submits a command through Runtime policy and returns its current Execution snapshot and available output.
- A wait timeout leaves the Execution running. Use `output` with its stable Cursor to read later output, or `kill` to terminate the Execution.

Working method
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
        "Tools",
        (
            "- The compact Tool catalog lists each discovered Tool by name, "
            "status, and summary only."
        ),
    ]
    if tool_catalog.entries:
        for entry in tool_catalog.entries:
            status = (
                "valid"
                if entry.valid
                else f"invalid: {entry.validation_error or 'unknown error'}"
            )
            lines.append(f"- {entry.name} ({status}): {entry.summary}")
    else:
        lines.append("- No Tools are currently discovered.")
    lines.append(
        "- Full documentation stays in the Tool files and is read on demand "
        'with `tools info <name>`.'
    )
    return "\n".join(lines)


def _render_skills_section(skill_catalog: _SkillCatalog) -> str:
    lines = [
        "Skills",
        (
            "- The compact Skill catalog lists each discovered Skill by name, "
            "status, and summary only."
        ),
    ]
    if skill_catalog.entries:
        for entry in skill_catalog.entries:
            status = (
                "valid"
                if entry.valid
                else f"invalid: {entry.validation_error or 'unknown error'}"
            )
            lines.append(f"- {entry.name} ({status}): {entry.summary}")
    else:
        lines.append("- No Skills are currently discovered.")
    lines.append(
        "- Full instructions stay in the Skill files and are read on demand "
        'with exec("cat .workspace/skills/<name>/SKILL.md").'
    )
    return "\n".join(lines)
