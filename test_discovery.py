import ipaddress
import threading
import unittest
from unittest.mock import MagicMock, patch

import discovery


class BroadcastTests(unittest.TestCase):
    def broadcast(self, interfaces, *, cycles=1, fail_first=False, connected=False,
                  updates=None):
        stop = threading.Event()
        client = threading.Event()
        if connected:
            client.set()
        sockets = []

        def socket_factory(*args):
            sock = MagicMock()
            sock.__enter__.return_value = sock
            if fail_first and len(sockets) == 1:
                sock.bind.side_effect = OSError("adapter removed")
            sockets.append(sock)
            return sock

        waits = []
        def wait(_timeout):
            waits.append(True)
            if len(waits) >= cycles:
                stop.set()

        with patch.object(discovery.sys, "platform", "win32"), \
                patch.object(discovery, "windows_lan_interfaces", return_value=interfaces) as lookup, \
                patch.object(discovery.socket, "socket", side_effect=socket_factory), \
                patch.object(stop, "wait", side_effect=wait):
            if updates is not None:
                lookup.side_effect = updates
            discovery.broadcast_receiver(5050, 5051, client, stop)
        return sockets, lookup

    def test_broadcasts_from_each_physical_address_to_its_actual_subnet(self):
        sockets, lookup = self.broadcast([ipaddress.IPv4Interface("172.20.10.6/28"),
                                          ipaddress.IPv4Interface("10.0.1.2/23")], cycles=2)
        lookup.assert_called_once()  # No PowerShell process every two seconds.
        sockets[0].sendto.assert_not_called()  # No route-dependent global broadcast.
        for sock, source, target in [(sockets[1], "172.20.10.6", "172.20.10.15"),
                                      (sockets[2], "10.0.1.2", "10.0.1.255")]:
            sock.bind.assert_called_once_with((source, 0))
            data, destination = sock.sendto.call_args.args
            self.assertEqual(destination, (target, 5051))
            self.assertEqual(discovery.parse_announcement(data, source), (source, 5050))

    def test_one_failed_adapter_does_not_prevent_other_adapter_broadcast(self):
        sockets, _ = self.broadcast([ipaddress.IPv4Interface("10.0.0.1/24"),
                                      ipaddress.IPv4Interface("172.20.10.6/28")], fail_first=True)
        sockets[1].sendto.assert_not_called()
        sockets[2].sendto.assert_called_once()

    def test_no_physical_adapters_never_falls_back_to_default_route(self):
        sockets, _ = self.broadcast([])
        self.assertEqual(len(sockets), 1)
        sockets[0].sendto.assert_not_called()

    def test_connected_service_does_not_broadcast(self):
        sockets, lookup = self.broadcast([ipaddress.IPv4Interface("10.0.0.1/24")], connected=True)
        lookup.assert_not_called()
        sockets[0].sendto.assert_not_called()

    def test_refresh_picks_up_changed_wifi_address(self):
        with patch.object(discovery.time, "monotonic", side_effect=[0, 0, 31, 31]):
            sockets, lookup = self.broadcast([], cycles=2, updates=[
                [ipaddress.IPv4Interface("10.0.0.2/24")],
                [ipaddress.IPv4Interface("172.20.10.6/28")]])
        self.assertEqual(lookup.call_count, 2)
        sockets[1].bind.assert_called_once_with(("10.0.0.2", 0))
        sockets[2].bind.assert_called_once_with(("172.20.10.6", 0))

    def test_point_to_point_interfaces_are_not_used_for_broadcast(self):
        sockets, _ = self.broadcast([ipaddress.IPv4Interface("10.0.0.2/31"),
                                      ipaddress.IPv4Interface("10.0.0.3/32")])
        self.assertEqual(len(sockets), 1)
        sockets[0].sendto.assert_not_called()

    def test_stop_during_interface_lookup_prevents_sending(self):
        stop = threading.Event()
        def lookup():
            stop.set()
            return [ipaddress.IPv4Interface("10.0.0.2/24")]
        with patch.object(discovery.sys, "platform", "win32"), \
                patch.object(discovery, "windows_lan_interfaces", side_effect=lookup), \
                patch.object(discovery.socket, "socket") as factory:
            discovery.broadcast_receiver(5050, 5051, threading.Event(), stop)
        factory.assert_called_once()
        factory.return_value.__enter__.return_value.sendto.assert_not_called()


if __name__ == "__main__":
    unittest.main()
