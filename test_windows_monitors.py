import ctypes
import unittest
from unittest.mock import Mock, patch

import windows_monitors as wm


def make_info(number, left, top, right, bottom, primary=False):
    info = wm.MonitorInfo()
    info.device = rf"\\.\DISPLAY{number}"
    info.monitor = wm.Rect(left, top, right, bottom)
    info.flags = 1 if primary else 0
    return info


class WindowsMonitorEnumerationTests(unittest.TestCase):
    def test_device_number_is_not_enumeration_order_or_primary_flag(self) -> None:
        # Sparse identifiers and a 64-bit handle exercise the Win32 ABI boundary.
        records = {0x100000003: make_info(7, 1920, -400, 4480, 1040),
                   0x100000001: make_info(2, 0, 0, 1920, 1080, True)}
        user32 = Mock()

        def get_info(handle, pointer):
            target = ctypes.cast(pointer, ctypes.POINTER(wm.MonitorInfo)).contents
            self.assertEqual(target.size, ctypes.sizeof(wm.MonitorInfo))
            ctypes.memmove(pointer, ctypes.byref(records[handle]), ctypes.sizeof(wm.MonitorInfo))
            return 1

        def enumerate_monitors(_dc, _rect, callback, _data):
            for handle in records:
                if not callback(handle, None, None, 0):
                    return 0
            return 1

        user32.GetMonitorInfoW.side_effect = get_info
        user32.EnumDisplayMonitors.side_effect = enumerate_monitors
        with patch.object(wm.sys, "platform", "win32"), patch.object(wm, "enable_dpi_awareness"), \
                patch.object(ctypes, "WinDLL", return_value=user32, create=True), \
                patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True):
            monitors = wm.list_monitors()
        self.assertEqual(list(monitors), [2, 7])
        self.assertTrue(monitors[2].primary)
        self.assertEqual(monitors[7].bounds.top, -400)
        self.assertIn("Monitor 7:", wm.describe_monitors(monitors)[1])

    def test_callback_errors_are_propagated_after_enumeration(self) -> None:
        user32 = Mock()

        def get_info(_handle, pointer):
            info = make_info(1, 0, 0, 0, 0)
            ctypes.memmove(pointer, ctypes.byref(info), ctypes.sizeof(info))
            return 1

        user32.GetMonitorInfoW.side_effect = get_info
        user32.EnumDisplayMonitors.side_effect = lambda dc, rect, cb, data: cb(1, None, None, 0)
        with patch.object(wm.sys, "platform", "win32"), patch.object(wm, "enable_dpi_awareness"), \
                patch.object(ctypes, "WinDLL", return_value=user32, create=True), \
                patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, create=True):
            with self.assertRaisesRegex(OSError, "Invalid dimensions"):
                wm.list_monitors()

    def test_non_display_device_is_rejected(self) -> None:
        info = make_info(1, 0, 0, 1920, 1080)
        info.device = "unknown"
        with self.assertRaisesRegex(OSError, "Unrecognized"):
            wm.monitor_from_info(info)

    def test_dpi_context_uses_pointer_sized_values(self) -> None:
        user32 = Mock()
        with patch.object(wm.sys, "platform", "win32"), \
                patch.object(ctypes, "WinDLL", return_value=user32, create=True):
            wm.enable_dpi_awareness()
        self.assertIs(user32.SetThreadDpiAwarenessContext.restype, ctypes.c_void_p)
        self.assertEqual(user32.SetProcessDpiAwarenessContext.call_args.args[0].value,
                         ctypes.c_void_p(-4).value)


if __name__ == "__main__":
    unittest.main()
