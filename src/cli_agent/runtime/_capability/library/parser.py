"""Library source parsers producing complete summary inputs.

Parsers consume raw bytes plus the logical filename; they never open Host
or Backend filesystem paths. Reading belongs to the Capability Catalog.
"""

from __future__ import annotations

from typing import Protocol


class LibraryParseError(ValueError):
    """Raised when one Library source cannot produce parser text."""


class LibraryFileParser(Protocol):
    """Format-aware extraction protocol isolated from the Library Catalog."""

    def supports(self, filename: str) -> bool:
        """Return whether this parser can extract text from ``filename``."""

    async def parse(self, content: bytes, filename: str) -> str:
        """Return the complete normalized text of one source payload.

        Raises:
            LibraryParseError: If the source cannot be decoded.
        """


class TextLibraryFileParser:
    """UTF-8 text parser for ``.md`` and ``.txt`` Library sources."""

    _SUPPORTED_EXTENSIONS = frozenset({".md", ".txt"})

    def supports(self, filename: str) -> bool:
        return _suffix(filename) in self._SUPPORTED_EXTENSIONS

    async def parse(self, content: bytes, filename: str) -> str:
        del filename
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LibraryParseError("file is not valid UTF-8") from exc
        return text.replace("\r\n", "\n").replace("\r", "\n")


_LIBRARY_FILE_PARSERS: tuple[LibraryFileParser, ...] = (TextLibraryFileParser(),)


def _select_parser(filename: str) -> LibraryFileParser | None:
    """Return the first registered parser supporting ``filename``, or None."""

    for parser in _LIBRARY_FILE_PARSERS:
        if parser.supports(filename):
            return parser
    return None


def _suffix(filename: str) -> str:
    """Return the dot-prefixed extension of one logical filename."""

    name = filename.rsplit("/", 1)[-1]
    base, dot, suffix = name.rpartition(".")
    del base
    return "." + suffix if dot else ""
