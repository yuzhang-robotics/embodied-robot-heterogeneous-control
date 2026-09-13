"""Fail-closed privacy checks for derived Phase 2 evidence records."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence


class PrivacyBoundaryError(ValueError):
    """A derived record contains private or unbounded material."""


_FORBIDDEN_KEYS = frozenset(
    {
        "path",
        "model_path",
        "file_path",
        "source_path",
        "contents",
        "content",
        "pid",
        "process_id",
        "command",
        "command_line",
        "raw_line",
        "prompt",
        "response",
        "response_text",
        "model_text",
        "audio",
        "image",
    }
)
_FALSE_PRIVACY_FLAGS = frozenset(
    {
        "path_recorded",
        "paths_recorded",
        "private_paths_recorded",
        "contents_recorded",
        "pid_recorded",
        "command_recorded",
        "raw_telemetry_recorded",
        "raw_input_recorded",
        "raw_model_text_recorded",
    }
)
_ABSOLUTE_PATH_RE = re.compile(r"(?:^[A-Za-z]:[\\/]|^\\\\|^/)")
_MAX_DEPTH = 32
_MAX_ITEMS = 100_000


def privacy_errors(value: object) -> list[str]:
    """Return bounded structural errors without echoing private values."""

    errors: list[str] = []
    visited_items = 0

    def visit(item: object, location: str, depth: int) -> None:
        nonlocal visited_items
        visited_items += 1
        if visited_items > _MAX_ITEMS:
            raise PrivacyBoundaryError("privacy scan item bound exceeded")
        if depth > _MAX_DEPTH:
            raise PrivacyBoundaryError("privacy scan depth bound exceeded")
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str) or not 1 <= len(key) <= 128:
                    errors.append(f"{location}: invalid record key")
                    continue
                child_location = f"{location}.{key}"
                lowered = key.lower()
                if lowered in _FORBIDDEN_KEYS:
                    errors.append(f"{child_location}: forbidden field")
                if lowered in _FALSE_PRIVACY_FLAGS and child is not False:
                    errors.append(f"{child_location}: privacy flag is not false")
                visit(child, child_location, depth + 1)
            return
        if isinstance(item, (bytes, bytearray, memoryview)):
            errors.append(f"{location}: binary content is forbidden")
            return
        if isinstance(item, str) and _ABSOLUTE_PATH_RE.search(item):
            errors.append(f"{location}: absolute private path is forbidden")
            return
        if isinstance(item, Sequence) and not isinstance(item, str):
            if len(item) > _MAX_ITEMS:
                raise PrivacyBoundaryError("privacy scan sequence bound exceeded")
            for index, child in enumerate(item):
                visit(child, f"{location}[{index}]", depth + 1)

    visit(value, "$", 0)
    return errors


def validate_privacy_boundary(value: object) -> None:
    """Raise unless a derived record contains no forbidden identity or content."""

    errors = privacy_errors(value)
    if errors:
        raise PrivacyBoundaryError("; ".join(errors[:16]))
