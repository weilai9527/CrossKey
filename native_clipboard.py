"""Native Mac/Windows clipboard formats, with portable payload conversion."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import io
import struct
import sys
import time
from typing import Any

from PIL import Image

from clipboard_formats import (
    MAX_PIXELS, RICH_MAX_BYTES, decode_cf_html, encode_cf_html, image_to_png,
    pack_files, pack_rich, png_to_dib, unpack_files, unpack_rich,
)

Content = tuple[str, bytes]
MAX_NATIVE_BYTES = MAX_PIXELS * 4 + 1024 * 1024


def _content(raw: dict[str, Any]) -> Content | None:
    if raw.get("files"):
        return "files", pack_files(raw["files"])
    text = raw.get("text", "")
    rich = raw.get("html") or raw.get("rtf")
    if rich and (text or not raw.get("image")):
        return "rich", pack_rich(text, raw.get("html"), raw.get("rtf"))
    if raw.get("image"):
        return "image", image_to_png(raw["image"])
    return ("text", text.encode("utf-8")) if text else None


class MacClipboard:
    def __init__(self, pasteboard_name: str | None = None) -> None:
        import AppKit
        import Foundation
        import objc
        self.app = AppKit
        self.foundation = Foundation
        self.objc = objc
        self.pasteboard_name = pasteboard_name

    def _pasteboard(self):
        if self.pasteboard_name:
            pb = self.app.NSPasteboard.pasteboardWithName_(self.pasteboard_name)
        else:
            pb = self.app.NSPasteboard.generalPasteboard()
        if pb is None:
            raise OSError("macOS pasteboard service is unavailable")
        return pb

    def revision(self) -> int:
        with self.objc.autorelease_pool():
            return int(self._pasteboard().changeCount())

    def read(self) -> Content | None:
        with self.objc.autorelease_pool():
            pb = self._pasteboard()
            types = set(pb.types() or [])
            raw: dict[str, Any] = {}
            if "public.file-url" in types or "NSFilenamesPboardType" in types:
                urls = pb.readObjectsForClasses_options_(
                    [self.foundation.NSURL],
                    {self.app.NSPasteboardURLReadingFileURLsOnlyKey: True},
                )
                raw["files"] = [str(url.path()) for url in (urls or []) if url.isFileURL()]
                if not raw["files"]:
                    raw["files"] = list(pb.propertyListForType_("NSFilenamesPboardType") or [])
            if not raw.get("files"):
                raw["text"] = str(pb.stringForType_("public.utf8-plain-text") or "")
                for key, candidates in (("image", ("public.png", "public.tiff")),
                                        ("html", ("public.html",)), ("rtf", ("public.rtf",))):
                    for name in candidates:
                        if name in types:
                            data = pb.dataForType_(name)
                            if data is not None:
                                limit = MAX_NATIVE_BYTES if key == "image" else RICH_MAX_BYTES
                                if len(data) > limit:
                                    raise ValueError("native clipboard data is too large")
                                raw[key] = bytes(data)
                                break
        return _content(raw)

    def write(self, kind: str, data: bytes) -> int:
        # Prepare and validate everything before replacing the clipboard.
        paths = unpack_files(data) if kind == "files" else None
        formats: dict[str, bytes] = {}
        if kind == "image":
            formats["public.png"] = image_to_png(data, png_only=True)
            with Image.open(io.BytesIO(formats["public.png"])) as image:
                tiff = io.BytesIO()
                image.save(tiff, "TIFF")
                formats["public.tiff"] = tiff.getvalue()
        elif kind == "rich":
            text, html, rtf = unpack_rich(data)
            formats["public.utf8-plain-text"] = text.encode("utf-8")
            if html:
                formats["public.html"] = html
            if rtf:
                formats["public.rtf"] = rtf
        elif kind == "text":
            data.decode("utf-8")
            formats["public.utf8-plain-text"] = data
        elif kind != "files":
            raise ValueError("unsupported clipboard kind")
        with self.objc.autorelease_pool():
            pb = self._pasteboard()
            if paths:
                objects = [self.foundation.NSURL.fileURLWithPath_(path) for path in paths]
            else:
                item = self.app.NSPasteboardItem.alloc().init()
                for name, value in formats.items():
                    nsdata = self.foundation.NSData.dataWithBytes_length_(value, len(value))
                    if not item.setData_forType_(nsdata, name):
                        raise OSError("could not prepare Mac clipboard data")
                objects = [item]
            pb.clearContents()
            if not pb.writeObjects_(objects):
                raise OSError("could not write Mac clipboard")
            return int(pb.changeCount())


class WindowsClipboard:
    def __init__(self) -> None:
        from ctypes import wintypes as w
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        # Explicit pointer-sized signatures are essential on 64-bit Windows.
        signatures = (
            (self.user, "OpenClipboard", [w.HWND], w.BOOL),
            (self.user, "CloseClipboard", [], w.BOOL),
            (self.user, "EmptyClipboard", [], w.BOOL),
            (self.user, "GetClipboardSequenceNumber", [], w.DWORD),
            (self.user, "IsClipboardFormatAvailable", [w.UINT], w.BOOL),
            (self.user, "GetClipboardData", [w.UINT], w.HANDLE),
            (self.user, "SetClipboardData", [w.UINT, w.HANDLE], w.HANDLE),
            (self.user, "RegisterClipboardFormatW", [w.LPCWSTR], w.UINT),
            (self.user, "CreateWindowExW", [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
             ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
             w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID], w.HWND),
            (self.user, "DestroyWindow", [w.HWND], w.BOOL),
            (self.kernel, "GlobalAlloc", [w.UINT, ctypes.c_size_t], w.HGLOBAL),
            (self.kernel, "GlobalLock", [w.HGLOBAL], w.LPVOID),
            (self.kernel, "GlobalUnlock", [w.HGLOBAL], w.BOOL),
            (self.kernel, "GlobalSize", [w.HGLOBAL], ctypes.c_size_t),
            (self.kernel, "GlobalFree", [w.HGLOBAL], w.HGLOBAL),
            (self.shell, "DragQueryFileW", [w.HANDLE, w.UINT, w.LPWSTR, w.UINT], w.UINT),
        )
        for dll, name, args, result in signatures:
            function = getattr(dll, name)
            function.argtypes, function.restype = args, result
        self.png = self.user.RegisterClipboardFormatW("PNG")
        self.html = self.user.RegisterClipboardFormatW("HTML Format")
        self.rtf = self.user.RegisterClipboardFormatW("Rich Text Format")
        self.drop_effect = self.user.RegisterClipboardFormatW("Preferred DropEffect")
        if not all((self.png, self.html, self.rtf, self.drop_effect)):
            raise ctypes.WinError(ctypes.get_last_error())

    def revision(self) -> int:
        return int(self.user.GetClipboardSequenceNumber())

    @contextmanager
    def _opened(self, *, writing: bool = False):
        # A short-lived hidden window gives EmptyClipboard a valid owner.
        hwnd = None
        if writing:
            hwnd = self.user.CreateWindowExW(0, "STATIC", "CrossKey Clipboard", 0,
                                             0, 0, 0, 0, -3, None, None, None)
            if not hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
        opened = False
        try:
            for _ in range(10):
                if self.user.OpenClipboard(hwnd):
                    opened = True
                    break
                time.sleep(0.02)
            if not opened:
                raise OSError("Windows clipboard is busy")
            yield
        finally:
            if opened:
                self.user.CloseClipboard()
            if hwnd:
                self.user.DestroyWindow(hwnd)

    def _get(self, format_id: int, limit: int = MAX_NATIVE_BYTES) -> bytes | None:
        if not self.user.IsClipboardFormatAvailable(format_id):
            return None
        handle = self.user.GetClipboardData(format_id)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        size = self.kernel.GlobalSize(handle)
        if size > limit:
            raise ValueError("native clipboard data is too large")
        pointer = self.kernel.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return ctypes.string_at(pointer, size)
        finally:
            self.kernel.GlobalUnlock(handle)

    def read(self) -> Content | None:
        raw: dict[str, Any] = {}
        with self._opened():
            if self.user.IsClipboardFormatAvailable(15):  # CF_HDROP
                handle = self.user.GetClipboardData(15)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                count = self.shell.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)
                if count > 2000:
                    raise ValueError("too many clipboard files")
                paths = []
                for index in range(count):
                    length = self.shell.DragQueryFileW(handle, index, None, 0)
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    self.shell.DragQueryFileW(handle, index, buffer, length + 1)
                    paths.append(buffer.value)
                raw["files"] = paths
            else:
                text = self._get(13, RICH_MAX_BYTES)  # CF_UNICODETEXT
                raw["text"] = text.decode("utf-16-le").split("\0", 1)[0] if text else ""
                html = self._get(self.html, RICH_MAX_BYTES)
                raw["html"] = decode_cf_html(html) if html else None
                rtf = self._get(self.rtf, RICH_MAX_BYTES)
                raw["rtf"] = rtf.rstrip(b"\0") if rtf else None
                raw["image"] = self._get(self.png) or self._get(8) or self._get(17)
        # File access and image decoding must happen after releasing the clipboard.
        return _content(raw)

    def _allocate(self, data: bytes) -> int:
        handle = self.kernel.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        pointer = self.kernel.GlobalLock(handle)
        if not pointer:
            self.kernel.GlobalFree(handle)
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(pointer, data, len(data))
        finally:
            self.kernel.GlobalUnlock(handle)
        return handle

    def write(self, kind: str, data: bytes) -> int:
        formats = {}
        if kind == "files":
            paths = unpack_files(data)
            # DROPFILES: offset, POINT, fNC, fWide, then UTF-16 double-NUL list.
            formats[15] = struct.pack("<IiiII", 20, 0, 0, 0, 1) + (
                "\0".join(paths) + "\0\0").encode("utf-16-le")
            formats[self.drop_effect] = struct.pack("<I", 1)  # Copy, never move.
        elif kind == "image":
            png = image_to_png(data, png_only=True)
            formats[self.png], formats[8] = png, png_to_dib(png)
        elif kind == "rich":
            text, html, rtf = unpack_rich(data)
            formats[13] = (text + "\0").encode("utf-16-le")
            if html:
                formats[self.html] = encode_cf_html(html)
            if rtf:
                formats[self.rtf] = rtf + b"\0"
        elif kind == "text":
            formats[13] = (data.decode("utf-8") + "\0").encode("utf-16-le")
        else:
            raise ValueError("unsupported clipboard kind")
        allocated = {}
        try:
            for format_id, value in formats.items():
                allocated[format_id] = self._allocate(value)
            with self._opened(writing=True):
                if not self.user.EmptyClipboard():
                    raise ctypes.WinError(ctypes.get_last_error())
                for format_id, handle in allocated.items():
                    if not self.user.SetClipboardData(format_id, handle):
                        raise ctypes.WinError(ctypes.get_last_error())
                    allocated[format_id] = None  # Ownership transferred to Windows.
            return self.revision()
        finally:
            for handle in allocated.values():
                if handle:
                    self.kernel.GlobalFree(handle)


def create_clipboard():
    if sys.platform == "darwin":
        return MacClipboard()
    if sys.platform == "win32":
        return WindowsClipboard()
    return None  # Retain the existing pyperclip text fallback on other platforms.
