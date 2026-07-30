"""cli-agent configuration boundary."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from cli_agent.runtime import OpenAICompatibleModelProvider

MODEL_ENV = "CLI_AGENT_MODEL"
BASE_URL_ENV = "CLI_AGENT_BASE_URL"
API_KEY_ENV = "CLI_AGENT_API_KEY"


class CliConfigurationError(ValueError):
    """Raised when cli-agent configuration is invalid."""


@dataclass(frozen=True, slots=True)
class CliConfig:
    """Validated configuration needed to open cli-agent."""

    task: str | None
    workspace: Path
    base_url: str
    model: str
    api_key: str = field(repr=False)
    repertoire: Path | None = None


def parse_cli_config(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> CliConfig:
    """Parse and validate cli-agent arguments and environment."""

    args = _argument_parser().parse_args(argv)
    environment = os.environ if environ is None else environ

    task = args.task.strip() if args.task is not None else None
    if task == "":
        raise CliConfigurationError("task must not be empty")

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise CliConfigurationError(
            f"workspace must be an existing directory: {args.workspace}"
        )
    repertoire = (
        None
        if args.repertoire is None
        else Path(args.repertoire).expanduser().resolve()
    )

    model = _required_environment(environment, MODEL_ENV)
    base_url = _normalize_base_url(_required_environment(environment, BASE_URL_ENV))
    api_key = _required_environment(environment, API_KEY_ENV)

    return CliConfig(
        task=task,
        workspace=workspace,
        base_url=base_url,
        model=model,
        api_key=api_key,
        repertoire=repertoire,
    )


def build_provider(
    config: CliConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> OpenAICompatibleModelProvider:
    """Construct the official Provider Adapter from validated CLI config."""

    return OpenAICompatibleModelProvider(
        model=config.model,
        api_key=config.api_key,
        base_url=config.base_url,
        transport=transport,
    )


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-agent",
        description="Run a task or start an interactive cli-agent session.",
    )
    parser.add_argument(
        "task",
        nargs="?",
        help="Task to run once; omit it to start an interactive session.",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace directory (default: current directory).",
    )
    parser.add_argument(
        "--repertoire",
        default=None,
        help=(
            "Repertoire directory "
            "(default: ~/.config/cli-agent/repertoire)."
        ),
    )
    return parser


def _required_environment(
    environment: Mapping[str, str],
    name: str,
) -> str:
    value = environment.get(name)
    if value is None or not value.strip():
        raise CliConfigurationError(
            f"environment variable {name} is not set; "
            "export it from .envrc and run direnv allow"
        )
    return value.strip()


def _normalize_base_url(value: str) -> str:
    base_url = value.strip().rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise CliConfigurationError(
            "base URL must be an absolute HTTP(S) URL without query or fragment"
        )
    return base_url
