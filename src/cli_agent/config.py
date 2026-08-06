"""cli-agent configuration boundary."""

from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from cli_agent.runtime import ContextPolicy, OpenAICompatibleModelProvider

MODEL_ENV = "CLI_AGENT_MODEL"
BASE_URL_ENV = "CLI_AGENT_BASE_URL"
API_KEY_ENV = "CLI_AGENT_API_KEY"
CONTEXT_WINDOW_ENV = "CLI_AGENT_CONTEXT_WINDOW"
OUTPUT_RESERVE_ENV = "CLI_AGENT_OUTPUT_RESERVE"
CONTEXT_SAFETY_MARGIN_ENV = "CLI_AGENT_CONTEXT_SAFETY_MARGIN"
DEFAULT_OUTPUT_RESERVE = 16_384
DEFAULT_CONTEXT_SAFETY_MARGIN = 4_096

MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "deepseek-v4-flash": 1_000_000,
}


class CliConfigurationError(ValueError):
    """Raised when cli-agent configuration is invalid."""


@dataclass(frozen=True, slots=True)
class CliConfig:
    """Validated configuration needed to open cli-agent."""

    task: str | None
    workspace: Path
    base_url: str
    model: str
    context_window_tokens: int
    output_reserve_tokens: int
    api_key: str = field(repr=False)
    repertoire: Path | None = None
    safety_margin_tokens: int = DEFAULT_CONTEXT_SAFETY_MARGIN


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
    context_window_tokens = _resolve_context_window(environment, model)
    output_reserve_tokens = _positive_int(
        environment,
        OUTPUT_RESERVE_ENV,
        default=DEFAULT_OUTPUT_RESERVE,
    )
    safety_margin_tokens = _non_negative_int(
        environment,
        CONTEXT_SAFETY_MARGIN_ENV,
        default=DEFAULT_CONTEXT_SAFETY_MARGIN,
    )
    if context_window_tokens <= output_reserve_tokens + safety_margin_tokens:
        raise CliConfigurationError(
            "context input budget must be positive: "
            f"{CONTEXT_WINDOW_ENV}={context_window_tokens} must exceed "
            f"{OUTPUT_RESERVE_ENV}={output_reserve_tokens} plus "
            f"{CONTEXT_SAFETY_MARGIN_ENV}={safety_margin_tokens}"
        )

    return CliConfig(
        task=task,
        workspace=workspace,
        base_url=base_url,
        model=model,
        api_key=api_key,
        repertoire=repertoire,
        context_window_tokens=context_window_tokens,
        output_reserve_tokens=output_reserve_tokens,
        safety_margin_tokens=safety_margin_tokens,
    )


def build_context_policy(config: CliConfig) -> ContextPolicy:
    """Construct the explicit Context budget policy from validated CLI config."""

    return ContextPolicy(
        context_window_tokens=config.context_window_tokens,
        output_reserve_tokens=config.output_reserve_tokens,
        safety_margin_tokens=config.safety_margin_tokens,
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
        help=("Repertoire directory (default: ~/.cli-agent/repertoire)."),
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


def _resolve_context_window(
    environment: Mapping[str, str],
    model: str,
) -> int:
    """Resolve the model Context Window from explicit config or the registry.

    An explicit ``CLI_AGENT_CONTEXT_WINDOW`` always wins. Otherwise the model's
    maximum known Context Window is used; models without a registry entry must
    be configured explicitly.
    """

    value = environment.get(CONTEXT_WINDOW_ENV)
    if value is not None and value.strip():
        return _positive_int(environment, CONTEXT_WINDOW_ENV)
    known = MODEL_CONTEXT_WINDOWS.get(model)
    if known is not None:
        return known
    raise CliConfigurationError(
        f"environment variable {CONTEXT_WINDOW_ENV} is not set and model "
        f"{model!r} has no known maximum context window"
    )


def _positive_int(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int | None = None,
) -> int:
    value = environment.get(name)
    if value is None or not value.strip():
        if default is not None:
            return default
        raise CliConfigurationError(
            f"environment variable {name} is not set; "
            "export it from .envrc and run direnv allow"
        )
    try:
        parsed = int(value)
    except ValueError:
        raise CliConfigurationError(
            f"environment variable {name} must be an integer, got {value!r}"
        ) from None
    if parsed <= 0:
        raise CliConfigurationError(
            f"environment variable {name} must be a positive integer, got {value!r}"
        )
    return parsed


def _non_negative_int(
    environment: Mapping[str, str],
    name: str,
    *,
    default: int,
) -> int:
    value = environment.get(name)
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError:
        raise CliConfigurationError(
            f"environment variable {name} must be an integer, got {value!r}"
        ) from None
    if parsed < 0:
        raise CliConfigurationError(
            f"environment variable {name} must be a non-negative integer, got {value!r}"
        )
    return parsed


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
