"""Security helpers for public logging, audit records, and pagination."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Mapping
from typing import Any

_SECRET_KEY = re.compile(
    r"(?:authorization|cookie|csrf|password|secret|token|api[_-]?key|credential)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:bsk_live_[A-Za-z0-9_-]+|Bearer\s+[A-Za-z0-9._~+/=-]+)",
    re.IGNORECASE,
)


def redact_payload(value: Any, *, key: str = "") -> Any:
    """Recursively redact likely secrets before logging or persistence."""
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_payload(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)[:1000]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:1000]


def encode_cursor(offset: int) -> str | None:
    """Return an opaque cursor for a non-negative row offset."""
    if offset <= 0:
        return None
    return base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    """Decode a cursor and reject malformed or negative values."""
    if cursor is None:
        return 0
    try:
        padding = "=" * (-len(cursor) % 4)
        value = int(base64.urlsafe_b64decode(cursor + padding).decode())
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError("invalid pagination cursor") from exc
    if value < 0:
        raise ValueError("invalid pagination cursor")
    return value
