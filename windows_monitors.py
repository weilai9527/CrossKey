"""Enumerate active Windows displays using their DISPLAYn device identifiers."""

from __future__ import annotations

import ctypes
import re
import sys
from dataclasses import dataclass

from screen_edges import ScreenBounds


@dataclass(frozen=True)
class WindowsMonitor:
    number: int
    device: str
    bounds: ScreenBounds
    primary: bool = False


class Rect(ctypes.Structure):
    _fields_ = [(name, ctypes.c_int32) for name in ("left", "top", "right", "bottom")]


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("monitor", Rect), ("work", Rect),
                ("flags", ctypes.c_uint32), ("device", ctypes.c_wchar * 32)]


def monitor_from_info(info: MonitorInfo) -> WindowsMonitor:
    match = re.fullmatch(r"\\\\\.\\DISPLAY([1-9][0-9]*)", info.device, re.IGNORECASE)
    if match is None:
        raise OSError(f"Unrecognized Windows display device: {info.device}")
    rect = info.monitor
    if rect.right - rect.left <= 1 or rect.bottom - rect.top <= 1:
        raise OSError(f"Invalid dimensions for {info.device}")
    return WindowsMonitor(int(match[1]), info.device,
                          ScreenBounds(rect.left, rect.top, rect.right, rect.bottom),
                          bool(info.flags & 1))


def enable_dpi_awareness() -> None:
    """Use physical pixels for both monitor rectangles and pynput coordinates."""
    if sys.platform != "win32":
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    try:
        set_process = user32.SetProcessDpiAwarenessContext
        set_process.argtypes = [ctypes.c_void_p]
        set_process.restype = ctypes.c_int32
        set_process(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        # A Python host/manifest may already have fixed the process default.
        set_thread = user32.SetThreadDpiAwarenessContext
        set_thread.argtypes = [ctypes.c_void_p]
        set_thread.restype = ctypes.c_void_p
        if not set_thread(ctypes.c_void_p(-4)):
            if not set_thread(ctypes.c_void_p(-3)):
                raise OSError("Cannot enable per-monitor DPI awareness")
    except AttributeError:
        try:
            ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            user32.SetProcessDPIAware()


def list_monitors() -> dict[int, WindowsMonitor]:
    if sys.platform != "win32":
        raise OSError("Monitor enumeration requires Windows")
    enable_dpi_awareness()
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(Rect), ctypes.c_ssize_t,
    )
    get_info = user32.GetMonitorInfoW
    get_info.argtypes = [ctypes.c_void_p, ctypes.POINTER(MonitorInfo)]
    get_info.restype = ctypes.c_int32
    enumerate_monitors = user32.EnumDisplayMonitors
    enumerate_monitors.argtypes = [ctypes.c_void_p, ctypes.POINTER(Rect),
                                  callback_type, ctypes.c_ssize_t]
    enumerate_monitors.restype = ctypes.c_int32
    monitors: dict[int, WindowsMonitor] = {}
    errors: list[Exception] = []

    @callback_type
    def collect(handle, _hdc, _rect, _data):
        # ctypes callbacks must not let Python exceptions escape into Win32.
        try:
            info = MonitorInfo()
            info.size = ctypes.sizeof(info)
            if not get_info(handle, ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            monitor = monitor_from_info(info)
            if monitor.number in monitors:
                raise OSError(f"Duplicate monitor number {monitor.number}; use extended display mode")
            monitors[monitor.number] = monitor
            return 1
        except Exception as exc:
            errors.append(exc)
            return 0

    success = enumerate_monitors(None, None, collect, 0)
    if errors:
        raise OSError(str(errors[0])) from errors[0]
    if not success:
        raise ctypes.WinError(ctypes.get_last_error())
    if not monitors:
        raise OSError("No active Windows monitors found")
    return dict(sorted(monitors.items()))


def describe_monitors(monitors: dict[int, WindowsMonitor]) -> list[str]:
    lines = []
    for number, monitor in sorted(monitors.items()):
        b = monitor.bounds
        lines.append(f"Monitor {number}: {monitor.device} "
                     f"{b.right - b.left:g}x{b.bottom - b.top:g} "
                     f"at ({b.left:g}, {b.top:g})" + (" [primary]" if monitor.primary else ""))
    return lines
