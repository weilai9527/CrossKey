from __future__ import annotations

import threading
import time
from typing import Any, Callable

import pyperclip


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
    ) -> None:
        self.send_event = send_event
        self.is_control_mode_enabled = is_control_mode_enabled
        self.local_source = local_source
        self.remote_name = remote_name
        self.enabled = enabled
        self.only_control_mode = only_control_mode
        self.poll_interval = poll_interval
        self.max_bytes = max_bytes
        self.last_seen_text: str | None = None
        self.last_remote_applied_text: str | None = None
        self.stop_event = threading.Event()
        self.lock = threading.Lock()

    def start(self) -> None:
        if self.enabled:
            print("Clipboard sync is ON", flush=True)
            threading.Thread(target=self._poll_loop, daemon=True).start()

    def stop(self) -> None:
        self.stop_event.set()

    def handle_remote_event(self, event: dict[str, Any]) -> None:
        if event.get("type") != "clipboard_text" or not self._should_sync_now():
            return
        if event.get("source") == self.local_source:
            return
        text = event.get("text")
        if not isinstance(text, str) or not self._text_size_allowed(text):
            return
        try:
            pyperclip.copy(text)
        except pyperclip.PyperclipException as exc:
            print(f"Clipboard write failed: {exc}", flush=True)
            return
        with self.lock:
            self.last_seen_text = text
            self.last_remote_applied_text = text
        print(f"Clipboard text received from {self.remote_name}: {len(text)} chars", flush=True)

    def _poll_loop(self) -> None:
        while not self.stop_event.wait(self.poll_interval):
            if not self._should_sync_now():
                continue
            try:
                text = pyperclip.paste()
            except pyperclip.PyperclipException as exc:
                print(f"Clipboard read failed: {exc}", flush=True)
                continue
            if not isinstance(text, str) or not text:
                continue
            if not self._text_size_allowed(text):
                with self.lock:
                    self.last_seen_text = text
                print("Clipboard text skipped: too large", flush=True)
                continue
            with self.lock:
                if text in {self.last_seen_text, self.last_remote_applied_text}:
                    continue
                self.last_seen_text = text
            event = {
                "type": "clipboard_text",
                "text": text,
                "clipboard_id": f"{self.local_source}-{time.time_ns()}",
                "source": self.local_source,
            }
            if self.send_event(event):
                print(f"Clipboard text sent to {self.remote_name}: {len(text)} chars", flush=True)

    def _should_sync_now(self) -> bool:
        return self.enabled and (
            not self.only_control_mode or self.is_control_mode_enabled()
        )

    def _text_size_allowed(self, text: str) -> bool:
        return len(text.encode("utf-8")) <= self.max_bytes
