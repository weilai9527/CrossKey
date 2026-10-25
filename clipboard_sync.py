from __future__ import annotations

import base64
import hashlib
import io
import threading
import time
import uuid
from typing import Any, Callable

import pyperclip

from clipboard_formats import unpack_rich
from native_clipboard import Content, create_clipboard
from protocol import CLIPBOARD_CHUNK_BYTES, CLIPBOARD_LIMITS, validate_event

SendEvent = Callable[[dict[str, Any]], bool]


class ClipboardSync:
    def __init__(
        self,
        send_event: SendEvent,
        is_control_mode_enabled: Callable[[], bool],
        local_source: str,
        remote_name: str,
        enabled: bool,
        only_control_mode: bool,
        poll_interval: float,
        max_bytes: int,
        *,
        backend: Any = None,
        supports_extended: Callable[[], bool] = lambda: True,
    ) -> None:
        self.send_event = send_event
        self.is_control_mode_enabled = is_control_mode_enabled
        self.local_source = local_source
        self.remote_name = remote_name
        self.enabled = enabled
        self.only_control_mode = only_control_mode
        self.poll_interval = poll_interval
        self.max_bytes = max_bytes
        self.backend = backend
        self._backend_initialized = backend is not None
        self.supports_extended = supports_extended
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self._revision: int | None = None
        self._cached: Content | None = None
        self._cached_signature: tuple[str, str] | None = None
        self._last_sent: tuple[str, str] | None = None
        self._outgoing: dict[str, Any] | None = None
        self._incoming: dict[str, Any] | None = None
        self._completed: tuple[str, bool] | None = None
        self._last_error = ""

    def _native(self):
        if not self._backend_initialized:
            self.backend = create_clipboard()
            self._backend_initialized = True
        return self.backend

    def start(self) -> None:
        if self.enabled:
            print("Clipboard sync is ON (text, images, files, rich text)", flush=True)
            threading.Thread(target=self._poll_loop, daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.lock:
            self._incoming = self._outgoing = None

    def reset_connection(self) -> None:
        with self.lock:
            self._incoming = self._outgoing = None
            self._completed = None
            # Re-send current local content after reconnect, including transfers
            # that had entered the send queue but had not been applied remotely.
            self._last_sent = None

    @staticmethod
    def _signature(content: Content) -> tuple[str, str]:
        return content[0], hashlib.sha256(content[1]).hexdigest()

    def handle_remote_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind not in {"clipboard_text", "clipboard_chunk", "clipboard_ack"}:
            return
        with self.lock:
            if not self._should_sync_now():
                self._incoming = None
                return
            if kind == "clipboard_ack":
                validate_event(event)
                if self._outgoing and event.get("clipboard_id") == self._outgoing["id"]:
                    self._last_sent = self._outgoing["signature"]
                    if event["accepted"]:
                        print(f"Clipboard {self._last_sent[0]} delivered to {self.remote_name}", flush=True)
                    self._outgoing = None
                    if not event.get("accepted"):
                        print(f"Clipboard transfer was not applied by {self.remote_name}; copy again to retry", flush=True)
                return
            if event.get("source") == self.local_source:
                return
            try:
                validate_event(event)
                if kind == "clipboard_text":
                    text = event["text"]
                    if not self._text_size_allowed(text):
                        return
                    self._incoming = None
                    self._apply("text", text.encode("utf-8"))
                elif self.supports_extended():
                    self._receive_chunk(event)
            except Exception as exc:
                self._incoming = None
                self._report_error(exc)
                if kind == "clipboard_chunk":
                    self._ack(str(event.get("clipboard_id", "")), False)

    def _ack(self, transfer_id: str, accepted: bool) -> None:
        self._completed = (transfer_id, accepted)
        self.send_event({"type": "clipboard_ack", "clipboard_id": transfer_id,
                         "accepted": accepted})

    def _receive_chunk(self, event: dict[str, Any]) -> None:
        transfer_id = event["clipboard_id"]
        if self._completed and self._completed[0] == transfer_id:
            self._ack(*self._completed)
            return
        now = time.monotonic()
        if self._incoming and now - self._incoming["updated"] > 60:
            self._incoming = None
        backend = self._native()
        if backend is None:
            self._ack(transfer_id, False)
            return
        if event["index"] == 0:
            self._incoming = {"id": transfer_id, "kind": event["kind"], "size": event["size"],
                              "sha256": event["sha256"], "source": event["source"],
                              "buffer": io.BytesIO(), "next": 0, "updated": now,
                              "revision": backend.revision()}
        incoming = self._incoming
        if not incoming or incoming["id"] != transfer_id:
            return
        if (event["index"] != incoming["next"] or any(
                event[key] != incoming[key] for key in ("kind", "size", "sha256", "source"))):
            raise ValueError("out-of-order clipboard transfer")
        incoming["buffer"].write(base64.b64decode(event["data"], validate=True))
        incoming["next"] += 1
        incoming["updated"] = now
        if incoming["buffer"].tell() != incoming["size"]:
            return
        data = incoming["buffer"].getvalue()
        self._incoming = None
        if hashlib.sha256(data).hexdigest() != incoming["sha256"]:
            raise ValueError("clipboard transfer checksum mismatch")
        if backend.revision() != incoming["revision"]:
            # A new local Copy during a long transfer takes precedence.
            self._ack(transfer_id, False)
            return
        self._apply(incoming["kind"], data)
        self._ack(transfer_id, True)

    def _apply(self, kind: str, data: bytes) -> None:
        backend = self._native()
        if backend is None:
            if kind != "text":
                raise ValueError("native clipboard formats are unavailable")
            pyperclip.copy(data.decode("utf-8"))
            self._revision = None
        else:
            self._revision = backend.write(kind, data)
        self._cached = kind, data
        self._cached_signature = self._signature(self._cached)
        self._last_sent = self._cached_signature
        self._outgoing = None
        print(f"Clipboard {kind} received from {self.remote_name}: {len(data)} bytes", flush=True)

    def _report_error(self, exc: Exception) -> None:
        message = str(exc)
        if message != self._last_error:
            print(f"Clipboard sync skipped: {message}", flush=True)
            self._last_error = message

    def _poll_loop(self) -> None:
        while not self.stop_event.wait(0.02 if self._outgoing else self.poll_interval):
            try:
                self._poll_once()
            except Exception as exc:
                # Clipboard contention or one unsupported format must not kill
                # the worker and silently disable future clipboard updates.
                self._report_error(exc)

    def _poll_once(self) -> None:
        with self.lock:
            if self._incoming and time.monotonic() - self._incoming["updated"] > 60:
                self._incoming = None
            if not self._should_sync_now():
                self._incoming = self._outgoing = None
                return
            backend = self._native()
            revision = backend.revision() if backend else None
            if revision is None or revision != self._revision:
                try:
                    if backend:
                        content = backend.read()
                        if backend.revision() != revision:
                            return  # Changed while being read/encoded; try a fresh snapshot.
                    else:
                        text = pyperclip.paste()
                        content = ("text", text.encode("utf-8")) if text else None
                except ValueError as exc:
                    self._revision, self._cached = revision, None
                    self._outgoing = None
                    self._report_error(exc)
                    return
                self._cached, self._revision = content, revision
                self._cached_signature = self._signature(content) if content else None
                # A fresh Copy is an explicit retry even when its bytes match a
                # previously rejected transfer. Our own writes cache their revision.
                if revision is not None:
                    self._last_sent = None
                    self._outgoing = None
                self._last_error = ""
            content = self._cached
            if content is None:
                self._last_sent = None
                self._outgoing = None
                return
            signature = self._cached_signature
            if signature == self._last_sent:
                return
            kind, data = content
            if kind == "text" or (kind == "rich" and not self.supports_extended()):
                if kind == "rich":
                    data = unpack_rich(data)[0].encode("utf-8")
                self._outgoing = None
                if len(data) > self.max_bytes:
                    self._last_sent = signature
                    print("Clipboard text skipped: too large", flush=True)
                elif self.send_event({"type": "clipboard_text", "text": data.decode("utf-8"),
                                      "clipboard_id": uuid.uuid4().hex, "source": self.local_source}):
                    self._last_sent = signature
                return
            if not self.supports_extended():
                self._report_error(ValueError("images, files and rich text require v0.6.0 on both computers"))
                return
            if not data or len(data) > CLIPBOARD_LIMITS[kind]:
                self._last_sent = signature
                self._report_error(ValueError(f"clipboard {kind} is too large"))
                return
            if not self._outgoing or self._outgoing["signature"] != signature:
                self._outgoing = {"id": uuid.uuid4().hex, "signature": signature,
                                  "data": data, "offset": 0, "sent_at": 0.0}
                print(f"Sending clipboard {kind} to {self.remote_name}: {len(data)} bytes", flush=True)
            outgoing = self._outgoing
            if outgoing["offset"] == len(data):
                if time.monotonic() - outgoing["sent_at"] < 30:
                    return
                outgoing["offset"] = 0  # Same ID lets an already-applied peer just re-ACK.
            for _ in range(4):
                offset = outgoing["offset"]
                if offset == len(data) or self.stop_event.is_set():
                    break
                chunk = data[offset:offset + CLIPBOARD_CHUNK_BYTES]
                event = {"type": "clipboard_chunk", "clipboard_id": outgoing["id"],
                         "source": self.local_source, "kind": kind, "size": len(data),
                         "sha256": signature[1], "index": offset // CLIPBOARD_CHUNK_BYTES,
                         "data": base64.b64encode(chunk).decode("ascii")}
                if not self.send_event(event):
                    break  # Queue pressure/disconnect: retry this chunk, without dropping it.
                outgoing["offset"] += len(chunk)
                outgoing["sent_at"] = time.monotonic()

    def _should_sync_now(self) -> bool:
        return self.enabled and (not self.only_control_mode or self.is_control_mode_enabled())

    def _text_size_allowed(self, text: str) -> bool:
        return len(text.encode("utf-8")) <= self.max_bytes
