import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import launcher_support
from launcher_support import BoundedLog, LauncherInstance, local_ipv4_addresses


class LocalAddressTests(unittest.TestCase):
    def test_mac_uses_assigned_up_addresses_and_ignores_inactive_link_local_loopback(self):
        output = b"""lo0: flags=8049<UP,LOOPBACK,RUNNING> mtu 16384
    inet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 172.20.10.9 netmask 0xfffffff0
    status: active
en1: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 192.168.8.9 netmask 0xffffff00
    status: inactive
en2: flags=8862<BROADCAST,RUNNING> mtu 1500
    inet 10.0.0.1 netmask 0xffffff00
en3: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 169.254.3.4 netmask 0xffff0000
en4: flags=8863<UP,BROADCAST,RUNNING> mtu 1500
    inet 172.20.10.9 netmask 0xfffffff0
"""
        with patch.object(launcher_support.sys, "platform", "darwin"), \
                patch.object(launcher_support.subprocess, "run") as run:
            run.return_value.stdout = output
            self.assertEqual(local_ipv4_addresses(), ["172.20.10.9"])
            self.assertEqual(run.call_args.args[0], ["/sbin/ifconfig", "-a"])

    def test_windows_localized_output_filters_invalid_addresses_and_keeps_multiple_adapters(self):
        output = """Windows IP 配置
   IPv4 地址 . . . . . . . . . . . . : 172.20.10.8(首选)
   IPv4 Address. . . . . . . . . . . : 10.0.0.4(Preferred)
   IPv4 地址 . . . . . . . . . . . . : 172.20.10.8
   自动配置 IPv4 地址 . . . . . . . : 169.254.1.2
   IPv4 Address: 127.0.0.1
   IPv4 Address: 0.0.0.0
   IPv4 Address: 0.9.8.7
   IPv4 Address: 224.0.0.1
   IPv4 Address: 255.255.255.255
   IPv4 Address: 999.0.0.1
""".encode()
        with patch.object(launcher_support.sys, "platform", "win32"), \
                patch.object(launcher_support.subprocess, "run") as run, \
                patch.object(launcher_support.socket, "getaddrinfo") as dns:
            run.return_value.stdout = output
            self.assertEqual(local_ipv4_addresses(), ["10.0.0.4", "172.20.10.8"])
            self.assertIn("ipconfig.exe", run.call_args.args[0][0])
            self.assertIn("creationflags", run.call_args.kwargs)
            dns.assert_not_called()

    def test_address_lookup_failures_are_empty_not_a_loopback_or_stale_guess(self):
        for error in (FileNotFoundError(), subprocess.TimeoutExpired("ifconfig", 3)):
            with self.subTest(error=type(error).__name__), \
                    patch.object(launcher_support.subprocess, "run", side_effect=error):
                self.assertEqual(local_ipv4_addresses(), [])


class LauncherInstanceTests(unittest.TestCase):
    def setUp(self):
        self.owner = LauncherInstance("mac", port=0)
        self.assertTrue(self.owner.acquire_or_notify())
        self.addCleanup(self.owner.close)

    def poll_until(self, condition):
        """TCP delivery can occur after sendall returns; the real UI also polls."""
        deadline = time.monotonic() + 2
        shown = False
        while time.monotonic() < deadline:
            shown = self.owner.poll_show() or shown
            if condition(shown):
                return shown
            time.sleep(0.005)
        self.fail("Launcher IPC did not reach the expected state within two seconds")

    def test_duplicate_acknowledged_after_poll_without_starting_second_owner(self):
        duplicate = LauncherInstance("mac", port=self.owner.port)
        self.addCleanup(duplicate.close)
        result = []
        errors = []

        def notify():
            try:
                result.append(duplicate.acquire_or_notify())
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=notify)
        thread.start()
        shown = False
        deadline = time.monotonic() + 2
        while thread.is_alive() and time.monotonic() < deadline:
            shown = self.owner.poll_show() or shown
            thread.join(0.01)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result, [False])
        self.assertTrue(shown)
        self.assertIsNone(duplicate._listener)

    def test_requests_are_bounded_and_partial_requests_do_not_block_polling(self):
        with socket.create_connection(("127.0.0.1", self.owner.port)) as conn:
            conn.sendall(b"CrossKey-launcher-")
            self.assertFalse(self.poll_until(lambda shown: len(self.owner._clients) == 1))
            self.assertEqual(len(self.owner._clients), 1)
            conn.sendall(b"show-v1\n")
            self.assertTrue(self.poll_until(lambda shown: shown))
            self.assertEqual(conn.recv(64), LauncherInstance._ACK)
            self.assertEqual(len(self.owner._clients), 0)
        with socket.create_connection(("127.0.0.1", self.owner.port)) as conn:
            conn.sendall(b"x" * 4096)
            self.assertFalse(self.poll_until(lambda shown: not self.owner._clients))
            self.assertEqual(len(self.owner._clients), 0)

    def test_silent_connection_is_expired_and_client_count_is_limited(self):
        clients = []
        try:
            for _ in range(10):
                clients.append(socket.create_connection(("127.0.0.1", self.owner.port)))
                self.assertFalse(self.owner.poll_show())
            self.assertLessEqual(len(self.owner._clients), LauncherInstance._MAX_CLIENTS)
            with patch.object(launcher_support.time, "monotonic", return_value=time.monotonic() + 4):
                self.assertFalse(self.owner.poll_show())
            self.assertEqual(len(self.owner._clients), 0)
        finally:
            for conn in clients:
                conn.close()

    def test_close_releases_ownership_for_next_launcher(self):
        replacement = LauncherInstance("mac", port=self.owner.port)
        self.addCleanup(replacement.close)
        self.owner.close()
        self.owner.close()
        self.assertFalse(self.owner.poll_show())
        self.assertTrue(replacement.acquire_or_notify())

    def test_missing_ack_is_an_error_not_silent_success(self):
        duplicate = LauncherInstance("mac", port=self.owner.port)
        with patch.object(launcher_support.socket, "create_connection") as connect:
            connect.return_value.__enter__.return_value.recv.return_value = b"NO\n"
            with self.assertRaisesRegex(OSError, "无法唤醒"):
                duplicate._notify_existing()

    def test_default_port_is_stable_across_copies(self):
        first = LauncherInstance("mac")
        second = LauncherInstance("mac")
        self.assertEqual(first.port, second.port)
        self.assertNotEqual(first.port, 5050)
        with self.assertRaises(ValueError):
            LauncherInstance("invalid")


class BoundedLogTests(unittest.TestCase):
    def test_long_lines_partial_lines_and_line_count_have_fixed_bounds(self):
        log = BoundedLog(max_lines=3, max_line_chars=32)
        self.assertEqual(log.write("a" * 100000), 100000)
        self.assertEqual(len(log._partial), 32)
        self.assertEqual(log.getvalue(), "")
        log.write("\nsecond\nthird\nfourth\n")
        self.assertEqual(log.lines(), ("second", "third", "fourth"))
        self.assertEqual(log.revision, 4)
        log.write("b" * 100 + "\n")
        self.assertEqual(len(log.lines()[-1]), 32)
        self.assertTrue(log.lines()[-1].endswith("…"))

    def test_pairing_codes_are_redacted_even_when_written_in_fragments(self):
        log = BoundedLog()
        log.write("Pairing code: ")
        log.write("123")
        self.assertNotIn("123", log.getvalue())
        log.write("456\n")
        log.write("{'pairing_code': '654321'}\n")
        log.write("配对码：987654\n")
        self.assertNotIn("123456", log.getvalue())
        self.assertNotIn("654321", log.getvalue())
        self.assertNotIn("987654", log.getvalue())
        self.assertEqual(log.getvalue().count("[hidden]"), 3)

    def test_concurrent_completed_lines_and_clear(self):
        log = BoundedLog()
        threads = [threading.Thread(target=lambda n=n: [log.write(f"{n}:{i}\n") for i in range(30)])
                   for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(log.lines()), 120)
        self.assertEqual(len(set(log.lines())), 120)
        self.assertFalse(log.isatty())
        self.assertTrue(log.writable())
        log.flush()
        log.clear()
        self.assertEqual(log.getvalue(), "")
        self.assertEqual(log.revision, 121)


if __name__ == "__main__":
    unittest.main()
