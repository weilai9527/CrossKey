"""Tiny newline-delimited JSON protocol for input events."""

from __future__ import annotations

import json
from typing import Any


ENCODING = "utf-8"


def encode_event(event: dict[str, Any]) -> bytes:
    """Encode one event as a JSON line."""
    return (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        ENCODING
    )


def decode_event(line: bytes) -> dict[str, Any]:
    """Decode one JSON line into an event dictionary."""
    value = json.loads(line.decode(ENCODING))
    if not isinstance(value, dict):
        raise ValueError("event must be a JSON object")
    return value
