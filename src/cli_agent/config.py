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

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY_ENV = "OPENAI_API_KEY"


class CliConfigurationError(ValueError):
    """Raised when cli-agent configuration is invalid."""


@dataclass(frozen=True, slots=True)
class CliConfig:
    """Validated configuration needed to open cli-agent."""

    task: str
    workspace: Path
    base_url: str
    model: str
    api_key_env: str
    api_key: str = field(repr=False)


def parse_cli_config(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> CliConfig:
    """Parse and validate cli-agent arguments and environment."""

    args = _argument_parser().parse_args(argv)
    environment = os.environ if environ is None else environ

    task = args.task.strip()
    if not task:
        raise CliConfigurationError("task must not be empty")

    model = args.model.strip()
    if not model:
        raise CliConfigurationError("model must not be empty")

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise CliConfigurationError(
            f"workspace must be an existing directory: {args.workspace}"
        )

    base_url = _normalize_base_url(args.base_url)
    api_key_env = args.api_key_env.strip()
    if not api_key_env:
        raise CliConfigurationError("API key environment variable name is empty")

    api_key = environment.get(api_key_env)
    if api_key is None or not api_key.strip():
        raise CliConfigurationError(
            f"API key environment variable {api_key_env} is not set; "
            "load it with direnv before starting cli-agent"
        )

    return CliConfig(
        task=task,
        workspace=workspace,
        base_url=base_url,
        model=model,
        api_key_env=api_key_env,
        api_key=api_key,
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
        description="Run one task with cli-agent.",
    )
    parser.add_argument("task", help="Task to submit to the Agent Runtime.")
    parser.add_argument(
        "--workspace",
        default=".",
        help="Workspace directory (default: current directory).",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"OpenAI-compatible API base URL (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Model name understood by the configured endpoint.",
    )
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help=(
            "Environment variable containing the Provider API key "
            f"(default: {DEFAULT_API_KEY_ENV}; normally loaded by direnv)."
        ),
    )
    return parser


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
