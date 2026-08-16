"""Private Environment Kernel implementation."""

from typing import Any

__all__ = ["EnvironmentKernel"]


def __getattr__(name: str) -> Any:
    """Load the Kernel lazily so handler facts cannot create import cycles."""

    if name != "EnvironmentKernel":
        raise AttributeError(name)
    from cli_agent.runtime._environment.kernel import EnvironmentKernel

    return EnvironmentKernel
