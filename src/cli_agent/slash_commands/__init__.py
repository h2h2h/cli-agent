"""Application-level slash command interfaces shared by Runner and TUI."""

from .catalog import CommandAction, CommandSpec, resolve, specs

__all__ = ["CommandAction", "CommandSpec", "resolve", "specs"]
