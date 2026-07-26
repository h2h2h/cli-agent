"""User-facing cli-agent entry point."""

from __future__ import annotations

import sys
from collections.abc import Sequence

from cli_agent.config import (
    CliConfigurationError,
    build_provider,
    parse_cli_config,
)


def main(argv: Sequence[str] | None = None) -> int:
    """Validate configuration for the cli-agent entry point."""

    try:
        config = parse_cli_config(argv)
        build_provider(config)
    except CliConfigurationError as exc:
        print(f"cli-agent: {exc}", file=sys.stderr)
        return 2
    return 0
