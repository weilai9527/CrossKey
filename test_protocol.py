import json
import unittest
from test_clipboard_sync import chunks

from protocol import (
    MAX_EVENT_LINE_BYTES,
    PROTOCOL_VERSION,
    decode_event,
    encode_event,
)


class ProtocolTests(unittest.TestCase):
    def test_clipboard_chunks_and_ack_round_trip(self) -> None:
        for kind in ("image", "files", "rich"):
            event = chunks(kind, b"binary\x00data")[0]
            self.assertEqual(decode_event(encode_event(event)), dict(event, protocol_version=1))
        ack = {"type": "clipboard_ack", "clipboard_id": "id", "accepted": True}
        self.assertEqual(decode_event(encode_event(ack)), dict(ack, protocol_version=1))

    def test_invalid_clipboard_chunks_are_rejected(self) -> None:
        event = chunks("image", b"png")[0]
        for key, value in (("kind", "executable"), ("size", 0), ("size", True),
                           ("size", 16 * 1024 * 1024 + 1), ("index", -1),
                           ("index", 1), ("index", True), ("sha256", "bad"),
                           ("data", "@@@="), ("data", "YQ==")):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                encode_event(dict(event, **{key: value}))
        with self.assertRaises(ValueError):
            encode_event({"type": "clipboard_ack", "clipboard_id": "id", "accepted": 1})

    def test_multi_monitor_events_and_rejections_round_trip(self) -> None:
        events = (
            {"type": "control_mode", "enabled": True, "edge": "top", "edge_margin": 2,
             "edge_monitors": {"top": 1, "right": 2}, "position": 0.25, "session_id": "current"},
            {"type": "control_rejected", "reason": "Monitor 2 unavailable", "session_id": "current"},
            {"type": "auth_ok", "capabilities": ["monitor_mapping_v1"]},
        )
        for event in events:
            self.assertEqual(decode_event(encode_event(event)), dict(event, protocol_version=1))

    def test_invalid_monitor_protocol_is_rejected(self) -> None:
        event = {"type": "control_mode", "enabled": True, "edge": "top", "edge_margin": 2,
                 "session_id": "current", "edge_monitors": {"top": 1, "right": 2}}
        for mapping in ({}, {"right": 2}, {"top": True}, {"top": "1"}, {"top": 0}, []):
            with self.subTest(mapping=mapping), self.assertRaises(ValueError):
                encode_event(dict(event, edge_monitors=mapping))
        with self.assertRaises(ValueError):
            encode_event(dict(event, enabled=False))
        for caps in ("monitor_mapping_v1", [None], ["x" * 65]):
            with self.assertRaises(ValueError):
                encode_event({"type": "auth_ok", "capabilities": caps})

    def test_round_trip_adds_protocol_version(self) -> None:
        event = {"type": "mouse_move", "dx": 2.5, "dy": -3}

        decoded = decode_event(encode_event(event))

        self.assertEqual(decoded["protocol_version"], PROTOCOL_VERSION)
        self.assertEqual(decoded["type"], "mouse_move")

    def test_heartbeat_round_trip(self) -> None:
        event = decode_event(encode_event({"type": "heartbeat"}))

        self.assertEqual(event["type"], "heartbeat")

    def test_edge_handoff_round_trip(self) -> None:
        for event in (
            {"type": "control_mode", "enabled": True, "edge": "left",
             "edge_margin": 2, "position": 0.25, "session_id": "session-1"},
            {"type": "return_control", "edge": "left",
             "position": 0.75, "session_id": "session-1"},
        ):
            with self.subTest(event=event):
                self.assertEqual(decode_event(encode_event(event)),
                                 dict(event, protocol_version=PROTOCOL_VERSION))

    def test_invalid_return_payloads_are_rejected(self) -> None:
        base = {"type": "return_control", "edge": "right",
                "position": 0.5, "session_id": "session-1"}
        for key, value in (("edge", "off"), ("edge", []), ("session_id", ""),
                           ("position", -0.1), ("position", 1.1),
                           ("position", True), ("position", float("nan"))):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                encode_event(dict(base, **{key: value}))

    def test_invalid_layout_payloads_are_rejected(self) -> None:
        base = {"type": "control_mode", "enabled": True, "edge": "right",
                "edge_margin": 2, "position": 0.5, "session_id": "session-1"}
        for key, value in (("edge", "off"), ("edge_margin", 0), ("edge_margin", 101),
                           ("edge_margin", True), ("edge_margin", 2.5),
                           ("session_id", ""), ("position", 2)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                encode_event(dict(base, **{key: value}))

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
