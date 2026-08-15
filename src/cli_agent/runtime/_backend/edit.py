"""Exact-text edit application shared by Backend Filesystem implementations."""

from __future__ import annotations

from cli_agent.runtime._backend.facts import _FileEdit


def apply_edits(content: str, edits: tuple[_FileEdit, ...], path: str) -> str:
    """Apply exact-text replacements on LF-normalized content.

    Every oldText is matched against the original content and must occur
    exactly once; replacements are then applied in reverse position order
    so offsets stay stable.

    Args:
        content (`str`):
            The original content normalized to LF line endings.
        edits (`tuple[_FileEdit, ...]`):
            Exact replacements to apply, matched against ``content``.
        path (`str`):
            The display path used in rejection messages.

    Returns:
        The fully applied content.

    Raises:
        ValueError: With an actionable message when an edit is empty, not
            found, duplicated, overlapping, or produces no change.
    """

    normalized = tuple(
        _FileEdit(
            old_text=edit.old_text.replace("\r\n", "\n"),
            new_text=edit.new_text.replace("\r\n", "\n"),
        )
        for edit in edits
    )
    total = len(normalized)
    for index, edit in enumerate(normalized):
        if not edit.old_text:
            raise ValueError(_empty_old_text_error(path, index, total))

    matches: list[tuple[int, int, int]] = []
    for index, edit in enumerate(normalized):
        start = content.find(edit.old_text)
        if start < 0:
            raise ValueError(_not_found_error(path, index, total))
        occurrences = content.count(edit.old_text)
        if occurrences > 1:
            raise ValueError(_duplicate_error(path, index, total, occurrences))
        matches.append((index, start, start + len(edit.old_text)))

    matches.sort(key=lambda match: match[1])
    for (previous, _, previous_end), (index, start, _) in zip(
        matches,
        matches[1:],
        strict=False,
    ):
        if previous_end > start:
            raise ValueError(_overlap_error(path, previous, index))

    updated = content
    for index, start, end in reversed(matches):
        updated = updated[:start] + normalized[index].new_text + updated[end:]
    if updated == content:
        raise ValueError(_no_change_error(path, total))
    return updated


def _empty_old_text_error(path: str, index: int, total: int) -> str:
    if total == 1:
        return f"oldText must not be empty in {path}."
    return f"edits[{index}].oldText must not be empty in {path}."


def _not_found_error(path: str, index: int, total: int) -> str:
    if total == 1:
        return (
            f"Could not find the exact text in {path}. The old text must match "
            "exactly including all whitespace and newlines."
        )
    return (
        f"Could not find edits[{index}] in {path}. The oldText must match exactly "
        "including all whitespace and newlines."
    )


def _duplicate_error(
    path: str,
    index: int,
    total: int,
    occurrences: int,
) -> str:
    if total == 1:
        return (
            f"Found {occurrences} occurrences of the text in {path}. The text "
            "must be unique. Please provide more context to make it unique."
        )
    return (
        f"Found {occurrences} occurrences of edits[{index}] in {path}. Each "
        "oldText must be unique. Please provide more context to make it unique."
    )


def _overlap_error(path: str, previous: int, current: int) -> str:
    return (
        f"edits[{previous}] and edits[{current}] overlap in {path}. Merge them "
        "into one edit or target disjoint regions."
    )


def _no_change_error(path: str, total: int) -> str:
    if total == 1:
        return (
            f"No changes made to {path}. The replacement produced identical "
            "content. This might indicate an issue with special characters or "
            "the text not existing as expected."
        )
    return f"No changes made to {path}. The replacements produced identical content."


def _split_bom(content: str) -> tuple[str, str]:
    """Return the leading BOM (if any) and the content without it."""

    if content.startswith("\ufeff"):
        return "\ufeff", content[1:]
    return "", content


def _detect_line_ending(content: str) -> str:
    """Return ``\\r\\n`` when the first newline is CRLF, else ``\\n``."""

    first_newline = content.find("\n")
    if first_newline > 0 and content[first_newline - 1] == "\r":
        return "\r\n"
    return "\n"
