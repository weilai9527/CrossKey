import io
from pathlib import Path
import stat
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from PIL import Image

from clipboard_formats import (
    decode_cf_html, encode_cf_html, image_to_png, pack_files, pack_rich,
    png_to_dib, unpack_files, unpack_rich,
)
from native_clipboard import _content


class ClipboardFormatTests(unittest.TestCase):
    def test_image_png_alpha_and_dib_compatibility(self):
        output = io.BytesIO()
        Image.new("RGBA", (11, 7), (200, 80, 40, 128)).save(output, "TIFF")
        png = image_to_png(output.getvalue())
        self.assertEqual(image_to_png(png, png_only=True), png)
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.getpixel((0, 0)), (200, 80, 40, 128))
        with Image.open(io.BytesIO(png_to_dib(png))) as image:
            self.assertEqual(image.size, (11, 7))
            self.assertEqual(image.mode, "RGB")

    def test_invalid_and_oversized_images_rejected(self):
        with self.assertRaises((OSError, ValueError)):
            image_to_png(b"not an image", png_only=True)
        output = io.BytesIO()
        Image.new("RGB", (2, 2)).save(output, "PNG")
        with patch("clipboard_formats.MAX_PIXELS", 3), self.assertRaises(ValueError):
            image_to_png(output.getvalue())

    def test_html_offsets_are_utf8_byte_offsets_and_preserve_context(self):
        fragment = '<b style="color:red">你好 🌍</b>'.encode()
        encoded = encode_cf_html(fragment)
        html = decode_cf_html(encoded)
        self.assertIn(fragment, html)
        self.assertEqual(decode_cf_html(encode_cf_html(html)), html)
        for line in encoded.split(b"\r\n")[:5]:
            if line.startswith(b"StartFragment:"):
                start = int(line.split(b":")[1])
            if line.startswith(b"EndFragment:"):
                end = int(line.split(b":")[1])
        self.assertEqual(encoded[start:end], fragment)
        with self.assertRaises(ValueError):
            decode_cf_html(b"StartHTML:99999\r\nEndHTML:999999\r\n")

    def test_rich_round_trip_with_unicode_html_rtf_and_plain_fallback(self):
        fields = ("中文", b"<b>bold</b>", br"{\rtf1\ansi hello}")
        self.assertEqual(unpack_rich(pack_rich(*fields)), fields)
        for invalid in (b"{}", b"[]", b'{"text": 1, "html":"", "rtf":""}',
                        b'{"text":"", "html":"@@", "rtf":""}'):
            with self.assertRaises(ValueError):
                unpack_rich(invalid)

    def test_files_and_nested_directories_round_trip_without_overwriting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            (source / "你好.txt").write_text("你好", encoding="utf-8")
            (source / "folder").mkdir()
            (source / "folder" / "data.bin").write_bytes(bytes(range(256)))
            (source / "folder" / "empty").mkdir()
            payload = pack_files([str(source / "你好.txt"), str(source / "folder")])
            paths = unpack_files(payload, directory=directory)
            paths2 = unpack_files(payload, directory=directory)
            self.assertNotEqual(paths, paths2)
            self.assertEqual(Path(paths[0]).read_text(encoding="utf-8"), "你好")
            self.assertEqual((Path(paths[1]) / "data.bin").read_bytes(), bytes(range(256)))
            self.assertTrue((Path(paths[1]) / "empty").is_dir())
            self.assertEqual((source / "你好.txt").read_text(encoding="utf-8"), "你好")

    def test_archive_rejects_traversal_absolute_paths_devices_and_links(self):
        for name in ("../escape", "/absolute", "C:/escape", "a/../../escape", "a\\escape",
                     "CON.txt", "a/file:stream", "a/trailing. "):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w") as archive:
                    archive.writestr(name, b"payload")
                with self.assertRaises(ValueError):
                    unpack_files(output.getvalue(), directory=directory)
                self.assertEqual(list(Path(directory).iterdir()), [])
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            entry = zipfile.ZipInfo("link")
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, b"../escape")
        with self.assertRaises(ValueError):
            unpack_files(output.getvalue())

    def test_archive_limits_and_symlink_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.bin"
            path.write_bytes(b"x" * 2000)
            with patch("clipboard_formats.FILES_MAX_BYTES", 1000), self.assertRaises(ValueError):
                pack_files([str(path)])
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("bomb", b"x" * 2000)
            with patch("clipboard_formats.FILES_MAX_BYTES", 1000), self.assertRaises(ValueError):
                unpack_files(output.getvalue(), directory=directory)
            link = Path(directory) / "link"
            try:
                link.symlink_to(path)
            except OSError:
                return  # Windows accounts may lack symlink privileges.
            with self.assertRaises(ValueError):
                pack_files([str(link)])

    def test_format_priority_preserves_rich_selection_over_bitmap_preview(self):
        result = _content({"text": "bold", "html": b"<b>bold</b>", "image": b"preview"})
        self.assertEqual(result[0], "rich")
        self.assertEqual(unpack_rich(result[1])[:2], ("bold", b"<b>bold</b>"))


if __name__ == "__main__":
    unittest.main()
