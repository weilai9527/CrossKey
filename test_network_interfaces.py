import ipaddress
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from network_interfaces import windows_lan_interfaces


class WindowsInterfaceTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "requires Windows PowerShell and network cmdlets")
    def test_real_windows_query_completes_with_valid_adapter_metadata(self):
        # Exercise the production command on Windows CI so syntax/cmdlet errors
        # cannot silently pass as an empty interface list in mocked tests.
        execute = subprocess.run
        results = []
        def capture(*args, **kwargs):
            result = execute(*args, **kwargs)
            results.append(result)
            return result
        with patch("network_interfaces.subprocess.run", side_effect=capture):
            windows_lan_interfaces()
        self.assertEqual(len(results), 1, "Windows adapter enumeration failed or timed out")
        records = json.loads(results[0].stdout.decode("utf-8-sig"))
        self.assertIsInstance(records, list)
        for record in records:
            self.assertIsInstance(record["HardwareInterface"], bool)
            self.assertIsInstance(record["Status"], str)

    def read(self, records):
        with patch("network_interfaces.subprocess.run") as run:
            run.return_value.stdout = json.dumps(records).encode()
            return windows_lan_interfaces()

    def record(self, address, prefix=24, hardware=True, status="Up", **extra):
        return dict(IPAddress=address, PrefixLength=prefix, HardwareInterface=hardware,
                    Status=status, **extra)

    def test_physical_wifi_and_ethernet_survive_virtual_adapters_and_renaming(self):
        records = [
            self.record("192.168.56.1", hardware=False, Name="VirtualBox"),
            self.record("192.168.80.1", hardware=False, Name="VMware VMnet8"),
            self.record("172.22.0.1", hardware=False, Name="vEthernet (WSL)"),
            self.record("10.8.0.1", hardware=False, Name="VPN"),
            self.record("192.168.99.1", hardware=False, Name="Ethernet"),
            self.record("172.20.10.6", 28, Name="无线网络连接"),
            self.record("10.0.0.4", Name="办公室有线"),
            self.record("172.20.10.6", 28),
            self.record("192.168.1.2", status="Disconnected"),
        ]
        self.assertEqual(self.read(records), [ipaddress.IPv4Interface("10.0.0.4/24"),
                                              ipaddress.IPv4Interface("172.20.10.6/28")])

    def test_invalid_and_non_lan_addresses_are_ignored(self):
        records = [self.record(ip) for ip in ["127.0.0.1", "169.254.1.2", "0.0.0.0",
                   "0.1.2.3", "224.0.0.1", "255.255.255.255", "999.0.0.1", "::1"]]
        records += [self.record("10.0.0.1", 99), {}, None,
                    self.record("10.0.0.2", hardware="true")]
        self.assertEqual(self.read(records), [])

    def test_failure_does_not_reintroduce_unfiltered_virtual_addresses(self):
        for error in [FileNotFoundError(), subprocess.TimeoutExpired("powershell", 5),
                      subprocess.CalledProcessError(1, "powershell")]:
            with self.subTest(error=error), patch("network_interfaces.subprocess.run", side_effect=error):
                self.assertEqual(windows_lan_interfaces(), [])
        with patch("network_interfaces.subprocess.run") as run:
            for output in [b"invalid json", b"null", b'{}', b'[]', b'\xff']:
                run.return_value.stdout = output
                self.assertEqual(windows_lan_interfaces(), [])


if __name__ == "__main__":
    unittest.main()
