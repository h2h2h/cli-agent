"""User-facing cli-agent entry point."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

from cli_agent.config import (
    CliConfigurationError,
    build_provider,
    parse_cli_config,
)
from cli_agent.runner import run_agent


def main(argv: Sequence[str] | None = None) -> int:
    """Run cli-agent in one-shot or interactive mode."""

    try:
        config = parse_cli_config(argv)
        provider = build_provider(config)
    except CliConfigurationError as exc:
        print(f"cli-agent: {exc}", file=sys.stderr)
        return 2

    try:
        return asyncio.run(
            run_agent(
                config,
                provider,
                stdin=sys.stdin,
                stdout=sys.stdout,
                stderr=sys.stderr,
            )
        )
    except KeyboardInterrupt:
        print("cli-agent: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"cli-agent: {exc}", file=sys.stderr)
        return 1
