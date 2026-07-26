"""Runtime-owned System Message assembly."""

from __future__ import annotations

from pathlib import Path

from runtime.model import SystemMessage


def assemble_system_message(
    workspace: Path,
    system_instruction: str | None,
) -> SystemMessage:
    """Build the stable instruction snapshot for a new Agent Session."""

    sections = [
        f"""You are cli-agent, an agent that completes tasks in a bound Workspace.

Workspace
- The bound Workspace is {workspace}.
- Commands start in this Workspace by default.
- The Workspace is an organizational boundary and default working directory, not an operating-system security boundary.

Built-in tools
- You can use exec, output, and kill according to their supplied schemas.
- exec runs a short command to completion and returns its execution record and available output.
- output reads a known execution record. kill addresses a known execution ID; current exec calls complete before returning.

Working method
- Inspect relevant state before making changes.
- Make only changes required by the task.
- Verify the result, then report the outcome concisely.""",
    ]
    if system_instruction is not None:
        sections.append(f"Host instruction\n{system_instruction}")

    return SystemMessage.text("\n\n".join(sections))
