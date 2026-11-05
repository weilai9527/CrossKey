import errno
import io
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import win_server


class StopServing(Exception):
    """Stop the mocked accept loop without opening a real listener."""


class ServerBindingTests(unittest.TestCase):
    def setUp(self):
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.server = MagicMock()
        self.server.accept.side_effect = StopServing
        self.socket_factory = self.patches.enter_context(patch.object(win_server.socket, "socket"))
        self.socket_factory.return_value.__enter__.return_value = self.server
        self.thread_factory = self.patches.enter_context(patch.object(win_server.threading, "Thread"))
        # This constant is exposed by Python only on Windows. Simulate it so the
        # exclusive-binding policy can also be checked on macOS/Linux CI.
        self.exclusive = 0x1234
        self.patches.enter_context(patch.object(win_server.socket, "SO_EXCLUSIVEADDRUSE",
                                                self.exclusive, create=True))
        self.output = io.StringIO()

    def serve(self):
        with redirect_stdout(self.output):
            win_server.serve("172.20.10.8", 5050, False, True, 0.7,
                             262144, 5051, "123456", 30)

    def test_windows_uses_only_exclusive_binding_before_bind_and_logs_identity(self):
        with patch.object(win_server.sys, "platform", "win32"), \
                patch.object(win_server.os, "getpid", return_value=4321):
            with self.assertRaises(StopServing):
                self.serve()

        self.assertEqual(self.server.method_calls[:3], [
            call.setsockopt(win_server.socket.SOL_SOCKET, self.exclusive, 1),
            call.bind(("172.20.10.8", 5050)),
            call.listen(1),
        ])
        self.server.setsockopt.assert_called_once()
        self.thread_factory.return_value.start.assert_called_once()
        self.assertIn("Receiver PID: 4321", self.output.getvalue())
        self.assertIn(f"Receiver script: {Path(win_server.__file__).resolve()}", self.output.getvalue())
        self.assertIn("Listening on 172.20.10.8:5050", self.output.getvalue())

    def test_other_platforms_keep_reuseaddr(self):
        with patch.object(win_server.sys, "platform", "darwin"):
            with self.assertRaises(StopServing):
                self.serve()
        self.server.setsockopt.assert_called_once_with(
            win_server.socket.SOL_SOCKET, win_server.socket.SO_REUSEADDR, 1)
        self.server.bind.assert_called_once_with(("172.20.10.8", 5050))

    def test_busy_or_reserved_windows_port_exits_with_original_error_and_no_broadcast(self):
        for number in (10048, 10013):
            with self.subTest(winerror=number):
                self.server.reset_mock()
                self.thread_factory.reset_mock()
                error = OSError(errno.EADDRINUSE if number == 10048 else errno.EACCES,
                                f"Windows bind failure {number}")
                error.winerror = number
                self.server.bind.side_effect = error
                with patch.object(win_server.sys, "platform", "win32"):
                    with self.assertRaises(SystemExit) as raised:
                        self.serve()
                self.assertIs(raised.exception.__cause__, error)
                message = str(raised.exception)
                self.assertIn(str(error), message)
                self.assertIn("172.20.10.8:5050", message)
                self.assertIn("Close the existing CrossKey Windows receiver", message)
                self.assertIn("netstat -ano", message)
                self.server.bind.assert_called_once_with(("172.20.10.8", 5050))
                self.server.listen.assert_not_called()
                self.thread_factory.assert_not_called()
                self.assertNotIn("Listening on", self.output.getvalue())

    def test_invalid_local_address_preserves_error_and_explains_interface_check(self):
        error = OSError(errno.EADDRNOTAVAIL, "Requested address is not local")
        self.server.bind.side_effect = error
        with patch.object(win_server.sys, "platform", "win32"):
            with self.assertRaises(SystemExit) as raised:
                self.serve()
        self.assertIs(raised.exception.__cause__, error)
        self.assertIn(str(error), str(raised.exception))
        self.assertIn("IP belongs to this computer", str(raised.exception))
        self.thread_factory.assert_not_called()

    def test_exclusive_option_failure_never_falls_back_to_address_reuse(self):
        error = OSError("Exclusive address use could not be configured")
        self.server.setsockopt.side_effect = error
        with patch.object(win_server.sys, "platform", "win32"):
            with self.assertRaises(OSError) as raised:
                self.serve()
        self.assertIs(raised.exception, error)
        self.server.setsockopt.assert_called_once_with(
            win_server.socket.SOL_SOCKET, self.exclusive, 1)
        self.server.bind.assert_not_called()
        self.thread_factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
