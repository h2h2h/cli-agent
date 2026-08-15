"""Application-level slash command interfaces shared by Runner and TUI."""

from .catalog import (
    CommandAction,
    CommandInvocation,
    CommandSpec,
    parse,
    resolve,
    specs,
)

__all__ = [
    "CommandAction",
    "CommandInvocation",
    "CommandSpec",
    "parse",
    "resolve",
    "specs",
]
