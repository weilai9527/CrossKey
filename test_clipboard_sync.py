import unittest
from unittest.mock import patch

from clipboard_sync import ClipboardSync


class ClipboardSyncTests(unittest.TestCase):
    def make_sync(self, sent: list[dict[str, object]], enabled: bool = True) -> ClipboardSync:
        return ClipboardSync(
            lambda event: not sent.append(event),
            lambda: enabled,
            "mac",
            "Windows",
            True,
            True,
            1.0,
            8,
        )

    def test_remote_text_is_applied(self) -> None:
        sync = self.make_sync([])
        with patch("clipboard_sync.pyperclip.copy") as copy:
            sync.handle_remote_event(
                {"type": "clipboard_text", "text": "hello", "source": "windows"}
            )
        copy.assert_called_once_with("hello")

    def test_oversized_remote_text_is_ignored(self) -> None:
        sync = self.make_sync([])
        with patch("clipboard_sync.pyperclip.copy") as copy:
            sync.handle_remote_event(
                {"type": "clipboard_text", "text": "123456789", "source": "windows"}
            )
        copy.assert_not_called()

    def test_clipboard_waits_for_control_mode(self) -> None:
        sync = self.make_sync([], enabled=False)
        with patch("clipboard_sync.pyperclip.copy") as copy:
            sync.handle_remote_event(
                {"type": "clipboard_text", "text": "hello", "source": "windows"}
            )
        copy.assert_not_called()


if __name__ == "__main__":
    unittest.main()
