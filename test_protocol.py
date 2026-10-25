import json
import unittest

from protocol import (
    MAX_EVENT_LINE_BYTES,
    PROTOCOL_VERSION,
    decode_event,
    encode_event,
)


class ProtocolTests(unittest.TestCase):
    def test_round_trip_adds_protocol_version(self) -> None:
        event = {"type": "mouse_move", "dx": 2.5, "dy": -3}

        decoded = decode_event(encode_event(event))

        self.assertEqual(decoded["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(decoded["type"], "mouse_move")

    def test_heartbeat_round_trip(self) -> None:
        event = decode_event(encode_event({"type": "heartbeat"}))

        self.assertEqual(event["type"], "heartbeat")

    def test_legacy_event_without_version_is_accepted(self) -> None:
        line = b'{"type":"control_mode","enabled":true}\n'

        self.assertTrue(decode_event(line)["enabled"])

    def test_unknown_event_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported event type"):
            decode_event(b'{"type":"run_command","command":"calc"}\n')

    def test_wrong_field_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "pressed must be a boolean"):
            encode_event(
                {"type": "mouse_click", "button": "left", "pressed": "yes"}
            )

    def test_non_finite_number_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "outside the allowed range"):
            encode_event({"type": "mouse_move", "dx": float("nan"), "dy": 0})

    def test_unsupported_protocol_version_is_rejected(self) -> None:
        line = json.dumps(
            {"protocol_version": 99, "type": "control_mode", "enabled": True}
        ).encode()

        with self.assertRaisesRegex(ValueError, "unsupported protocol version"):
            decode_event(line)

    def test_oversized_line_is_rejected_before_json_parsing(self) -> None:
        with self.assertRaisesRegex(ValueError, "too large"):
            decode_event(b"x" * (MAX_EVENT_LINE_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
