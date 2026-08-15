"""Low-level attach/exec stream helpers for the Docker Backend.

aiodocker exposes the hijacked container/exec stream through its private
``Stream`` object; the public surface covers ``write_in`` and ``read_out``
but no half-close. Docker treats the TCP FIN (``write_eof``) as EOF on the
container stdin while keeping stdout/stderr flowing, which is the only way
to run stdin-consuming commands like ``cat`` to completion.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aiodocker.stream import Stream


def _write_stdin_eof(stream: Stream) -> None:
    """Signal EOF on one hijacked stream without closing its read side.

    Args:
        stream (`Stream`): The aiodocker stream to half-close.

    Raises:
        RuntimeError: If the stream has no upgraded connection yet.
    """

    response = stream._resp  # type: ignore[attr-defined]
    if response is None:
        raise RuntimeError("Docker stream is not connected")
    connection = response.connection
    if connection is None:
        raise RuntimeError("Docker stream has no underlying connection")
    transport = connection.transport
    if transport is None:
        raise RuntimeError("Docker stream has no underlying transport")
    transport.write_eof()
