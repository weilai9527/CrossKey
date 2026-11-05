"""Small, standard-library-only helpers for the desktop launcher.

No browser runtime, background worker or external network probe is started here.
The launcher's UI loop polls the single-instance socket only while it is alive.
"""
from __future__ import annotations

from collections import deque
import errno
import getpass
import hashlib
import ipaddress
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time


def _usable_ipv4(values: list[str]) -> list[str]:
    addresses = set()
    for value in values:
        try:
            address = ipaddress.IPv4Address(value)
        except ipaddress.AddressValueError:
            continue
        if (address.is_loopback or address.is_link_local or address.is_multicast
                or address.is_unspecified or int(address) >> 24 == 0
                or address == ipaddress.IPv4Address("255.255.255.255")):
            continue
        addresses.add(address)
    return [str(address) for address in sorted(addresses)]


def local_ipv4_addresses() -> list[str]:
    """Read assigned local IPv4 addresses without DNS or an Internet probe.

    More than one address is legitimate (Wi-Fi, Ethernet, VPN, etc.). The UI
    should list these addresses rather than guessing that one is reachable by
    the other computer. No persistent helper process is created.
    """
    try:
        if sys.platform == "win32":
            command = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                          / "System32" / "ipconfig.exe")
            output = subprocess.run(
                [command], capture_output=True, check=False, timeout=3,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.decode("ascii", errors="ignore")
            # The IPv4 token and address are ASCII even in localized ipconfig.
            values = re.findall(r"IPv4[^\r\n:]*:\s*(\d+\.\d+\.\d+\.\d+)", output)
        elif sys.platform == "darwin":
            output = subprocess.run(
                ["/sbin/ifconfig", "-a"], capture_output=True, check=False,
                timeout=3,
            ).stdout.decode("ascii", errors="ignore")
            values = []
            for block in re.split(r"(?m)(?=^\S[^\n]*: flags=)", output):
                first_line = block.partition("\n")[0]
                if not re.search(r"[<,]UP[,>]", first_line):
                    continue
                if re.search(r"(?m)^\s*status: inactive\s*$", block):
                    continue
                values.extend(re.findall(r"(?m)^\s*inet\s+(\d+\.\d+\.\d+\.\d+)", block))
        else:
            output = subprocess.run(
                ["ip", "-o", "-4", "addr", "show", "up"],
                capture_output=True, check=False, timeout=3,
            ).stdout.decode("ascii", errors="ignore")
            values = re.findall(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", output)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return _usable_ipv4(values)


class LauncherInstance:
    """Own one per-user, per-role launcher, or ask its window to reappear.

    acquire_or_notify returns True for the owner and False after the existing
    owner acknowledges a show request. Other bind/IPC failures raise OSError.
    Call poll_show on the GUI thread about once a second, even with no window.
    Only a fixed show message is accepted; no paths, secrets or commands travel
    over this loopback-only endpoint.
    """
    _SHOW = b"CrossKey-launcher-show-v1\n"
    _ACK = b"CrossKey-launcher-ack-v1\n"
    _MAX_CLIENTS = 8
    _CLIENT_TIMEOUT = 3.0

    def __init__(self, role: str, *, port: int | None = None) -> None:
        if role not in {"mac", "windows"}:
            raise ValueError("role must be mac or windows")
        if port is None:
            # Stable across project copies and Python's randomized hash seed.
            user = str(os.getuid()) if hasattr(os, "getuid") else getpass.getuser()
            digest = hashlib.sha256(f"CrossKey:{user}:{role}".encode()).digest()
            port = 42000 + int.from_bytes(digest[:2], "big") % 18000
        if not 0 <= port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        self.port = port
        self._listener: socket.socket | None = None
        self._clients: dict[socket.socket, tuple[bytearray, float]] = {}

    def acquire_or_notify(self) -> bool:
        if self._listener is not None:
            return True
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform == "win32":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", self.port))
            listener.listen(self._MAX_CLIENTS)
            listener.setblocking(False)
        except OSError as exc:
            listener.close()
            if (exc.errno != errno.EADDRINUSE
                    and getattr(exc, "winerror", None) not in {10048, 10013}):
                raise
            self._notify_existing()
            return False
        self.port = listener.getsockname()[1]
        self._listener = listener
        return True

    def _notify_existing(self) -> None:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=3.0) as conn:
                conn.sendall(self._SHOW)
                response = bytearray()
                deadline = time.monotonic() + 3.0
                while len(response) < len(self._ACK):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Existing launcher did not acknowledge")
                    conn.settimeout(remaining)
                    chunk = conn.recv(len(self._ACK) - len(response))
                    if not chunk:
                        break
                    response.extend(chunk)
                if bytes(response) != self._ACK:
                    raise OSError("The local port is not responding as a CrossKey launcher")
        except OSError as exc:
            raise OSError(
                f"无法唤醒已有 CrossKey 窗口（本机端口 {self.port}）。"
                "请检查已有窗口是否仍在响应，然后重试。"
            ) from exc

    def poll_show(self) -> bool:
        listener = self._listener
        if listener is None:
            return False
        for _ in range(self._MAX_CLIENTS):
            try:
                conn, _ = listener.accept()
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                break
            if len(self._clients) >= self._MAX_CLIENTS:
                conn.close()
                continue
            conn.setblocking(False)
            self._clients[conn] = (bytearray(), time.monotonic())
        show = False
        for conn, (buffer, started) in list(self._clients.items()):
            close = time.monotonic() - started >= self._CLIENT_TIMEOUT
            if not close:
                try:
                    chunk = conn.recv(len(self._SHOW) + 1 - len(buffer))
                    if not chunk:
                        close = True
                    else:
                        buffer.extend(chunk)
                        if bytes(buffer) == self._SHOW:
                            conn.sendall(self._ACK)
                            show = True
                            close = True
                        elif not self._SHOW.startswith(buffer):
                            close = True
                except (BlockingIOError, InterruptedError):
                    pass
                except OSError:
                    close = True
            if close:
                self._clients.pop(conn, None)
                conn.close()
        return show

    def close(self) -> None:
        for conn in self._clients:
            conn.close()
        self._clients.clear()
        if self._listener is not None:
            self._listener.close()
            self._listener = None


class BoundedLog:
    """Thread-safe text sink; bounded complete lines and one bounded partial line.

    Assign it to stdout/stderr only in the launcher. Reading the buffer never
    reveals the partial line, so a pairing code split across write calls cannot
    appear briefly before the complete log line is redacted.
    """
    encoding = "utf-8"
    errors = "replace"
    _PAIRING = re.compile(r"(?:pairing[\s_-]*code[\"']?\s*[:=]|配对码\s*[:：=])", re.I)

    def __init__(self, max_lines: int = 200, max_line_chars: int = 2000) -> None:
        if max_lines < 1 or max_line_chars < 32:
            raise ValueError("Log requires at least one line and 32 characters per line")
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._partial = ""
        self._truncated = False
        self._max_line_chars = max_line_chars
        self._lock = threading.Lock()
        self._revision = 0

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @staticmethod
    def isatty() -> bool:
        return False

    @staticmethod
    def writable() -> bool:
        return True

    @staticmethod
    def flush() -> None:
        return None

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("write() argument must be str")
        with self._lock:
            start = 0
            while start < len(text):
                newline = text.find("\n", start)
                end = len(text) if newline == -1 else newline
                room = self._max_line_chars - len(self._partial)
                self._partial += text[start:min(end, start + room)]
                if end - start > room:
                    self._truncated = True
                if newline == -1:
                    break
                line = self._partial.rstrip("\r")
                marker = self._PAIRING.search(line)
                if marker:
                    line = line[:marker.end()] + " [hidden]"
                elif self._truncated:
                    line = line[:self._max_line_chars - 1] + "…"
                self._lines.append(line[:self._max_line_chars])
                self._partial = ""
                self._truncated = False
                self._revision += 1
                start = newline + 1
        return len(text)

    def lines(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._lines)

    def getvalue(self) -> str:
        with self._lock:
            return "\n".join(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._partial = ""
            self._truncated = False
            self._revision += 1
