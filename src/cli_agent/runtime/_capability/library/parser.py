"""Library source parsers producing complete summary inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class LibraryParseError(ValueError):
    """Raised when one Library source cannot produce parser text."""


class LibraryFileParser(Protocol):
    """Format-aware extraction protocol isolated from the Library Catalog."""

    def supports(self, path: Path) -> bool:
        """Return whether this parser can extract text from ``path``."""

    async def parse(self, path: Path) -> str:
        """Return the complete normalized text of ``path``.

        Raises:
            LibraryParseError: If the source cannot be read or decoded.
        """


class TextLibraryFileParser:
    """UTF-8 text parser for ``.md`` and ``.txt`` Library sources."""

    _SUPPORTED_EXTENSIONS = frozenset({".md", ".txt"})

    def supports(self, path: Path) -> bool:
        return path.suffix in self._SUPPORTED_EXTENSIONS

    async def parse(self, path: Path) -> str:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise LibraryParseError(f"cannot read file: {exc}") from exc
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise LibraryParseError("file is not valid UTF-8") from exc
        return text.replace("\r\n", "\n").replace("\r", "\n")


_LIBRARY_FILE_PARSERS: tuple[LibraryFileParser, ...] = (TextLibraryFileParser(),)


def _select_parser(path: Path) -> LibraryFileParser | None:
    """Return the first registered parser supporting ``path``, or None."""

    for parser in _LIBRARY_FILE_PARSERS:
        if parser.supports(path):
            return parser
    return None
