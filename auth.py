from __future__ import annotations

import hashlib
import hmac
import secrets
import socket

from protocol import MAX_EVENT_LINE_BYTES, decode_event, encode_event


class AuthenticationError(OSError):
    pass


def validate_pairing_code(code: str) -> str:
    value = str(code).strip()
    if not (value.isdigit() and len(value) == 6):
        raise ValueError("pairing_code must contain exactly 6 digits")
    return value


def generate_pairing_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _digest(code: str, nonce: str) -> str:
    return hmac.new(code.encode("utf-8"), nonce.encode("ascii"), hashlib.sha256).hexdigest()


def _read_event(sock: socket.socket) -> dict[str, object]:
    data = bytearray()
    while len(data) <= MAX_EVENT_LINE_BYTES:
        chunk = sock.recv(1)
        if not chunk:
            raise AuthenticationError("connection closed during authentication")
        data.extend(chunk)
        if chunk == b"\n":
            break
    if len(data) > MAX_EVENT_LINE_BYTES:
        raise AuthenticationError("authentication message is too large")
    try:
        return decode_event(bytes(data))
    except (UnicodeDecodeError, ValueError) as exc:
        raise AuthenticationError(f"invalid authentication message: {exc}") from exc


def authenticate_client(sock: socket.socket, pairing_code: str) -> None:
    pairing_code = validate_pairing_code(pairing_code)
    previous_timeout = sock.gettimeout()
    sock.settimeout(10)
    try:
        challenge = _read_event(sock)
        if challenge.get("type") != "auth_challenge":
            raise AuthenticationError("receiver did not start authentication")
        nonce = str(challenge["nonce"])
        sock.sendall(encode_event({"type": "auth_response", "digest": _digest(pairing_code, nonce)}))
        result = _read_event(sock)
        if result.get("type") != "auth_ok":
            raise AuthenticationError("pairing code was rejected")
    finally:
        sock.settimeout(previous_timeout)


def authenticate_server(conn: socket.socket, pairing_code: str) -> None:
    pairing_code = validate_pairing_code(pairing_code)
    nonce = secrets.token_hex(32)
    previous_timeout = conn.gettimeout()
    conn.settimeout(10)
    try:
        conn.sendall(encode_event({"type": "auth_challenge", "nonce": nonce}))
        response = _read_event(conn)
        if response.get("type") != "auth_response" or not hmac.compare_digest(
            str(response.get("digest", "")), _digest(pairing_code, nonce)
        ):
            raise AuthenticationError("pairing code was rejected")
        conn.sendall(encode_event({"type": "auth_ok"}))
    finally:
        conn.settimeout(previous_timeout)
