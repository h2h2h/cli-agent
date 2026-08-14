"""Consumer-neutral base for every classified cli-agent error."""

from __future__ import annotations

from collections.abc import Mapping


class CliAgentError(Exception):
    """Base class for every classified cli-agent error.

    Classified errors are organized by the consumer that can handle them,
    never by the module where they originate. Every classified error
    carries a stable structured payload:

    - ``code``: stable machine-readable semantic; public codes never use a
      lower-level module name as their namespace.
    - ``message``: sanitized audience-readable summary. It never leaks
      host paths, credentials, provider payloads, or tracebacks.
    - ``details``: serializable structured context for the consumer.

    Payload fields must stay JSON-serializable because boundaries persist
    and forward them: error objects never embed underlying exception
    instances, which instead live in Diagnostics and ``__cause__``.

    Args:
        code (`str`): Stable machine-readable error semantic.
        message (`str`): Sanitized audience-readable summary.
        details (`Mapping[str, object] | None`): Optional serializable
            structured context for the error consumer.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: Mapping[str, object] = dict(details or {})
