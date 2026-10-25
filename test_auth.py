import socket
import threading
import unittest

from auth import AuthenticationError, authenticate_client, authenticate_server


class AuthenticationTests(unittest.TestCase):
    def test_matching_pairing_codes_authenticate(self) -> None:
        server, client = socket.socketpair()
        errors: list[Exception] = []
        peers = []

        def run_server() -> None:
            try:
                peers.append(authenticate_server(server, "123456", capabilities=("monitor_mapping_v1",)))
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run_server)
        thread.start()
        try:
            result = authenticate_client(client, "123456", capabilities=("clipboard_v2",))
            self.assertEqual(result["capabilities"], ["monitor_mapping_v1"])
        finally:
            client.close()
            server.close()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(peers[0]["capabilities"], ["clipboard_v2"])

    def test_wrong_pairing_code_is_rejected(self) -> None:
        server, client = socket.socketpair()

        def run_server() -> None:
            try:
                authenticate_server(server, "123456")
            except AuthenticationError:
                server.sendall(b'{"type":"control_mode","enabled":false}\n')

        thread = threading.Thread(target=run_server)
        thread.start()
        try:
            with self.assertRaisesRegex(AuthenticationError, "rejected"):
                authenticate_client(client, "654321")
        finally:
            client.close()
            server.close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
