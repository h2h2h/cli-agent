"""Classification and retention guards for public Kernel/Runtime boundaries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from typing import TypeAlias

from cli_agent.errors.base import CliAgentError
from cli_agent.errors.host import InternalRuntimeError

DiagnosticSink: TypeAlias = Callable[[str, str, Mapping[str, object]], None]

INTERNAL_ERROR_DIAGNOSTIC_KIND = "error.internal"
_INTERNAL_ERROR_MESSAGE = "An internal runtime error occurred."


def internal_from_exception(
    exc: BaseException,
    *,
    operation: str,
) -> InternalRuntimeError:
    """Classify one unexpected exception into a sanitized internal error.

    The original exception is never embedded in the returned payload:
    only the failing operation and the exception type name are retained,
    so a Host can correlate the error with the full Diagnostic record.

    Args:
        exc (`BaseException`): The unexpected exception caught at the
            boundary.
        operation (`str`): Stable boundary operation identifier, for
            example ``kernel.dispatch``.

    Returns:
        The sanitized `InternalRuntimeError` to raise ``from`` the
        original exception.
    """

    return InternalRuntimeError(
        code="internal_error",
        message=_INTERNAL_ERROR_MESSAGE,
        details={
            "operation": operation,
            "exception_type": type(exc).__name__,
        },
    )


@contextmanager
def error_boundary(
    operation: str,
    *,
    sink: DiagnosticSink | None = None,
    passthrough: tuple[type[BaseException], ...] = (),
) -> Iterator[None]:
    """Retain classified failures and sanitize unexpected exceptions.

    Classified `CliAgentError` instances, cancellation, and the caller's
    declared legacy exception types re-raise unchanged, so an already
    classified error is never wrapped a second time. Any other
    `Exception` is reported through ``sink`` with full detail
    and re-raised as a sanitized `InternalRuntimeError`, so an unexpected
    exception never leaks an arbitrary underlying error across a public
    Kernel or Runtime boundary. Expected failures that are expressed as
    plain return values are not intercepted at all.

    Args:
        operation (`str`): Stable boundary operation identifier used in
            the error payload and Diagnostic.
        sink (`DiagnosticSink | None`): Optional sink receiving
            one ``("error.internal", message, detail)`` record before the
            sanitized error is raised.
        passthrough (`tuple[type[BaseException], ...]`): Additional
            exception types that intentionally cross this boundary today;
            owning issues remove entries as they reclassify them.

    Yields:
        None while the guarded block runs.
    """

    try:
        yield
    except (CliAgentError, asyncio.CancelledError, *passthrough):
        raise
    except Exception as exc:
        if sink is not None:
            sink(
                INTERNAL_ERROR_DIAGNOSTIC_KIND,
                f"{operation} raised an unexpected exception",
                {
                    "operation": operation,
                    "exception_type": type(exc).__name__,
                    "exception": repr(exc),
                },
            )
        raise internal_from_exception(exc, operation=operation) from exc
