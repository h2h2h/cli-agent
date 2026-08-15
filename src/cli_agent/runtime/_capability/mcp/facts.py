"""Pure-data MCP server configuration facts shared across Runtime layers."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Mapping

import jsonschema

_MAX_SERVER_NAME_LENGTH = 64

_MCP_CONFIG_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "transport"],
    "properties": {
        "name": {"type": "string"},
        "transport": {"enum": ["stdio", "http"]},
        "command": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
        "url": {"type": "string", "minLength": 1},
        "env": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "headers": {
            "type": "object",
            "additionalProperties": {"type": "string", "minLength": 1},
        },
    },
}


@dataclass(frozen=True, slots=True)
class _MCPToolFacts:
    """Provider-neutral facts for one discovered Workspace MCP tool."""

    name: str
    description: str
    input_schema: dict[str, object]


@dataclass(frozen=True, slots=True)
class _MCPServerFacts:
    """Provider-neutral facts for one discovered Workspace MCP server."""

    name: str
    tools: tuple[_MCPToolFacts, ...]


@dataclass(frozen=True, slots=True)
class MCPServerConfig:
    """One validated MCP server connection description.

    Only env variable names are recorded, never literal values; the executing
    worker resolves them from the effective child environment at runtime.
    """

    name: str
    transport: Literal["stdio", "http"]
    command: tuple[str, ...] | None
    url: str | None
    env: tuple[str, ...]
    headers: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, object]:
        """Render a serializable projection without resolving any env value."""

        return {
            "name": self.name,
            "transport": self.transport,
            "command": list(self.command) if self.command is not None else None,
            "url": self.url,
            "env": list(self.env),
            "headers": dict(self.headers),
        }


def parse_server_config(
    raw: object,
    *,
    directory_name: str,
) -> tuple[MCPServerConfig | None, tuple[str, ...]]:
    """Validate one raw MCP server config mapping.

    Args:
        raw (`object`):
            The parsed JSON value of one ``_mcp/<server>/config.json``.
        directory_name (`str`):
            The ``_mcp`` subdirectory name; ``name`` must match it.

    Returns:
        The validated config, or None with aggregated structural errors.
    """

    if not isinstance(raw, Mapping):
        return None, ("MCP server config must be a JSON object",)

    errors = list(_schema_errors(raw))
    if errors:
        return None, tuple(errors)

    name = str(raw["name"])
    transport = str(raw["transport"])
    errors.extend(_validate_server_name(name, directory_name))

    command = raw.get("command")
    command_tuple = tuple(command) if command is not None else None
    url = raw.get("url")
    env = tuple(raw.get("env") or ())
    headers = tuple(
        (key, value)
        for key, value in (raw.get("headers") or {}).items()
    )

    if transport == "stdio" and command_tuple is None:
        errors.append("stdio transport requires a non-empty 'command'")
    if transport == "http" and not url:
        errors.append("http transport requires a non-empty 'url'")

    if errors:
        return None, tuple(errors)

    return (
        MCPServerConfig(
            name=name,
            transport=transport,  # type: ignore[arg-type]
            command=command_tuple,
            url=url,
            env=env,
            headers=headers,
        ),
        (),
    )


def load_server_config(
    path: Path,
) -> tuple[MCPServerConfig | None, tuple[str, ...]]:
    """Read and validate one ``_mcp/<server>/config.json`` file.

    Args:
        path (`Path`):
            The ``config.json`` path beneath a ``_mcp/<server>`` directory.

    Returns:
        The validated config, or None with aggregated errors when the file is
        not readable UTF-8 JSON or fails structural validation.
    """

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, (f"MCP server config is not readable JSON: {exc}",)
    return parse_server_config(raw, directory_name=path.parent.name)


def _schema_errors(raw: object) -> tuple[str, ...]:
    validator = jsonschema.Draft202012Validator(_MCP_CONFIG_SCHEMA)
    return tuple(
        f"MCP server config is invalid: {error.message}"
        for error in validator.iter_errors(raw)
    )


def _validate_server_name(name: str, directory_name: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", name.strip())
    errors: list[str] = []
    if len(normalized) > _MAX_SERVER_NAME_LENGTH:
        errors.append(
            f"server name exceeds {_MAX_SERVER_NAME_LENGTH} character limit"
        )
    if normalized != normalized.lower():
        errors.append("server name must be lowercase")
    if normalized.startswith("-") or normalized.endswith("-"):
        errors.append("server name cannot start or end with a hyphen")
    if "--" in normalized:
        errors.append("server name cannot contain consecutive hyphens")
    if not all(
        character.isalnum() or character == "-" for character in normalized
    ):
        errors.append("server name may contain only letters, digits, and hyphens")

    directory = unicodedata.normalize("NFKC", directory_name)
    if normalized != directory:
        errors.append(
            f"server name {normalized!r} must match the directory name "
            f"{directory!r}"
        )
    return errors
