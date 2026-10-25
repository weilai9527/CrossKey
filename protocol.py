"""Tiny newline-delimited JSON protocol for input events."""

from __future__ import annotations

import json
import math
from typing import Any


ENCODING = "utf-8"
PROTOCOL_VERSION = 1
MAX_EVENT_LINE_BYTES = 2 * 1024 * 1024

EVENT_TYPES = {
    "auth_challenge",
    "auth_response",
    "auth_ok",
    "clipboard_text",
    "control_mode",
    "heartbeat",
    "return_control",
    "key_down",
    "key_up",
    "mouse_click",
    "mouse_move",
    "mouse_scroll",
}


def _require_bool(event: dict[str, Any], field: str) -> None:
    if not isinstance(event.get(field), bool):
        raise ValueError(f"{field} must be a boolean")


def _require_number(event: dict[str, Any], field: str) -> None:
    value = event.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    if not math.isfinite(value) or abs(value) > 1_000_000:
        raise ValueError(f"{field} is outside the allowed range")


def _require_string(
    event: dict[str, Any], field: str, *, allow_empty: bool = False, max_length: int = 256
) -> None:
    value = event.get(field)
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{field} must not be empty")
    if len(value) > max_length:
        raise ValueError(f"{field} is too long")


def validate_event(event: dict[str, Any]) -> None:
    """Validate the common wire shape without changing event contents."""
    version = event.get("protocol_version", PROTOCOL_VERSION)
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise ValueError(f"unsupported protocol version: {version!r}")

    event_type = event.get("type")
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unsupported event type: {event_type!r}")

    if event_type == "control_mode":
        _require_bool(event, "enabled")
        if "session_id" in event:
            _require_string(event, "session_id", max_length=64)
        if "edge" in event:
            _require_string(event, "session_id", max_length=64)
            _require_string(event, "edge", max_length=16)
            if event["edge"] not in {"left", "right", "top", "bottom"}:
                raise ValueError("invalid edge")
            margin = event.get("edge_margin")
            if type(margin) is not int or not 1 <= margin <= 100:
                raise ValueError("edge_margin must be an integer between 1 and 100")
        if "position" in event:
            if "edge" not in event:
                raise ValueError("position requires edge")
            _require_number(event, "position")
            if not 0 <= event["position"] <= 1:
                raise ValueError("position must be between 0 and 1")
    elif event_type == "return_control":
        _require_string(event, "session_id", max_length=64)
        _require_string(event, "edge", max_length=16)
        if event.get("edge") not in {"left", "right", "top", "bottom"}:
            raise ValueError("invalid edge")
        _require_number(event, "position")
        if not 0 <= event["position"] <= 1:
            raise ValueError("position must be between 0 and 1")
    elif event_type in {"mouse_move", "mouse_scroll"}:
        _require_number(event, "dx")
        _require_number(event, "dy")
    elif event_type == "mouse_click":
        _require_string(event, "button", max_length=32)
        _require_bool(event, "pressed")
    elif event_type in {"key_down", "key_up"}:
        if event.get("char") is not None:
            _require_string(event, "char", max_length=16)
        else:
            _require_string(event, "key", max_length=64)
    elif event_type == "clipboard_text":
        _require_string(
            event, "text", allow_empty=True, max_length=MAX_EVENT_LINE_BYTES
        )
        for field in ("clipboard_id", "source"):
            if field in event:
                _require_string(event, field, max_length=256)
    elif event_type == "auth_challenge":
        _require_string(event, "nonce", max_length=128)
    elif event_type == "auth_response":
        _require_string(event, "digest", max_length=128)


def encode_event(event: dict[str, Any]) -> bytes:
    """Encode one event as a JSON line."""
    wire_event = dict(event)
    wire_event.setdefault("protocol_version", PROTOCOL_VERSION)
    validate_event(wire_event)
    data = (
        json.dumps(wire_event, ensure_ascii=False, separators=(",", ":")) + "\n"
    ).encode(ENCODING)
    if len(data) > MAX_EVENT_LINE_BYTES:
        raise ValueError("encoded event is too large")
    return data


def decode_event(line: bytes) -> dict[str, Any]:
    """Decode one JSON line into an event dictionary."""
    if len(line) > MAX_EVENT_LINE_BYTES:
        raise ValueError("encoded event is too large")
    value = json.loads(line.decode(ENCODING))
    if not isinstance(value, dict):
        raise ValueError("event must be a JSON object")
    validate_event(value)
    return value
