"""Opt-in native integration uses a private named Mac pasteboard, never General."""
import io
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from PIL import Image

from clipboard_formats import image_to_png, pack_files, pack_rich, unpack_rich
from native_clipboard import MacClipboard, WindowsClipboard


@unittest.skipUnless(sys.platform == "darwin" and os.environ.get("CROSSKEY_NATIVE_TEST") == "1",
                     "set CROSSKEY_NATIVE_TEST=1 for private Mac pasteboard integration")
class MacNativeClipboardTests(unittest.TestCase):
    def setUp(self):
        self.backend = MacClipboard("com.crosskey.test." + uuid.uuid4().hex)

    def tearDown(self):
        with self.backend.objc.autorelease_pool():
            self.backend._pasteboard().releaseGlobally()

    def test_native_image_round_trip(self):
        output = io.BytesIO()
        Image.new("RGBA", (13, 9), (20, 40, 60, 100)).save(output, "PNG")
        data = image_to_png(output.getvalue())
        revision = self.backend.write("image", data)
        self.assertEqual(self.backend.revision(), revision)
        self.assertEqual(self.backend.read(), ("image", data))

    def test_native_rich_text_and_plain_fallback(self):
        data = pack_rich("你好", "<b>你好</b>".encode(), br"{\rtf1\ansi hello}")
        self.backend.write("rich", data)
        kind, result = self.backend.read()
        self.assertEqual(kind, "rich")
        self.assertEqual(unpack_rich(result), unpack_rich(data))
        self.backend.write("text", b"hello")
        self.assertEqual(self.backend.read(), ("text", b"hello"))

    def test_native_file_urls_contain_actual_received_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.txt"
            path.write_text("clipboard file")
            payload = pack_files([str(path)])
            self.backend.write("files", payload)
            with self.backend.objc.autorelease_pool():
                urls = self.backend._pasteboard().readObjectsForClasses_options_(
                    [self.backend.foundation.NSURL],
                    {self.backend.app.NSPasteboardURLReadingFileURLsOnlyKey: True})
                copied = Path(str(urls[0].path()))
            try:
                self.assertEqual(copied.read_text(), "clipboard file")
                self.assertEqual(self.backend.read()[0], "files")
            finally:
                shutil.rmtree(copied.parent)


class WindowsNativeResourceTests(unittest.TestCase):
    def make_backend(self):
        backend = WindowsClipboard.__new__(WindowsClipboard)
        backend.user, backend.kernel = Mock(), Mock()
        backend.user.CreateWindowExW.return_value = 99
        backend.user.OpenClipboard.return_value = True
        backend.user.SetClipboardData.return_value = 1
        backend.user.GetClipboardSequenceNumber.return_value = 3
        backend._allocate = Mock(side_effect=[101, 102, 103])
        backend.png, backend.html, backend.rtf, backend.drop_effect = 1001, 1002, 1003, 1004
        return backend

    def test_windows_owner_and_memory_ownership_on_success(self):
        backend = self.make_backend()
        self.assertEqual(backend.write("text", b"hello"), 3)
        backend.user.OpenClipboard.assert_called_once_with(99)
        backend.user.SetClipboardData.assert_called_once_with(13, 101)
        backend._allocate.assert_called_once_with("hello\0".encode("utf-16-le"))
        backend.kernel.GlobalFree.assert_not_called()
        backend.user.CloseClipboard.assert_called_once()
        backend.user.DestroyWindow.assert_called_once_with(99)

    def test_windows_formats_preserve_html_rtf_and_text(self):
        backend = self.make_backend()
        backend.write("rich", pack_rich("中文", b"<b>bold</b>", br"{\rtf1 bold}"))
        self.assertEqual([c.args[0] for c in backend.user.SetClipboardData.call_args_list], [13, 1002, 1003])

    def test_windows_hdrop_contains_unicode_double_null_paths_and_copy_effect(self):
        backend = self.make_backend()
        with patch("native_clipboard.unpack_files", return_value=["C:\\temp\\中文.txt"]):
            backend.write("files", b"archive")
        data = backend._allocate.call_args_list[0].args[0]
        self.assertEqual(data[:4], (20).to_bytes(4, "little"))
        self.assertEqual(data[16:20], (1).to_bytes(4, "little"))
        self.assertEqual(data[20:].decode("utf-16-le"), "C:\\temp\\中文.txt\0\0")
        self.assertEqual(backend._allocate.call_args_list[1].args[0], b"\x01\0\0\0")

    def test_untransferred_memory_is_freed_if_open_fails(self):
        backend = self.make_backend()
        backend.user.OpenClipboard.return_value = False
        with patch("native_clipboard.time.sleep"), self.assertRaises(OSError):
            backend.write("text", b"hello")
        backend.kernel.GlobalFree.assert_called_once_with(101)
        backend.user.CloseClipboard.assert_not_called()
        backend.user.DestroyWindow.assert_called_once_with(99)


if __name__ == "__main__":
    unittest.main()
