import unittest
from unittest.mock import patch

import app


class FakeSocket:
    def __init__(self):
        self.sent = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def sendall(self, data):
        self.sent.append(data)


class TimecodeTests(unittest.TestCase):
    def test_one_second_is_25_frames(self):
        self.assertEqual(app.tc_text_to_25fps_frames("00:00:01:00"), 25)

    def test_50fps_timecode_is_converted_to_25fps_base(self):
        self.assertEqual(app.tc_text_to_25fps_frames("00:00:01:20", is_50fps=True), 35)

    def test_frame_value_above_24_is_treated_as_50fps(self):
        self.assertEqual(app.tc_text_to_25fps_frames("00:00:00:48"), 24)

    def test_invalid_timecodes_are_rejected(self):
        for value in ("", "bad", "24:00:00:00", "00:60:00:00", "00:00:60:00"):
            with self.subTest(value=value):
                self.assertIsNone(app.tc_text_to_25fps_frames(value))

    def test_frames_are_formatted_as_doubled_timecode(self):
        self.assertEqual(app.frames_to_tcx2(25 + 3), "00:00:01:06")

    def test_timecode_wraps_after_24_hours(self):
        self.assertEqual(app.frames_to_tcx2(24 * 60 * 60 * 25), "00:00:00:00")

    def test_timecode_difference_format(self):
        self.assertEqual(app.format_tc_diff(2), "+2f")
        self.assertEqual(app.format_tc_diff(-2), "-2f")
        self.assertEqual(app.format_tc_diff(0), "0f")
        self.assertEqual(app.format_tc_diff(51), "+50+f")


class ProtocolTests(unittest.TestCase):
    def setUp(self):
        with app.CONFIGURATION_INFO_LOCK:
            app.CONFIGURATION_INFO_CACHE.clear()
        with app.FORMAT_SLOT_STATUS_LOCK:
            app.FORMAT_SLOT_STATUS_CACHE.clear()

    def test_protocol_fields_are_parsed(self):
        response = "208 transport info:\r\nstatus: record\r\ninput video format: 1080p50\r\n"
        info = app.parse_hyperdeck_info(response)
        self.assertEqual(info["status"], "record")
        self.assertEqual(info["input video format"], "1080p50")

    def test_recording_transport_response(self):
        response = (
            "208 transport info:\r\n"
            "status: record\r\n"
            "input video format: 1080p50\r\n"
            "display timecode: 01:02:03:20\r\n\r\n"
        )
        state = app.transport_response_to_state(response)
        self.assertEqual(state[0], "RECORDING")
        self.assertEqual(state[1], "recording")
        self.assertEqual(state[2], "1080p50")
        self.assertTrue(state[4])

    def test_no_signal_transport_response(self):
        response = "208 transport info:\r\nstatus: stopped\r\ninput video format: none\r\n\r\n"
        state = app.transport_response_to_state(response)
        self.assertEqual(state[:2], ("NO SIGNAL", "warning"))
        self.assertFalse(state[4])

    def test_invalid_transport_response_is_rejected(self):
        with self.assertRaises(OSError):
            app.transport_response_to_state("500 connection info:\r\n\r\n")

    def test_active_slot_is_parsed(self):
        self.assertEqual(app.transport_response_to_active_slot("208 transport info:\r\nslot id: 2\r\n"), 2)
        self.assertIsNone(app.transport_response_to_active_slot("208 transport info:\r\nslot id: 3\r\n"))

    def test_configuration_waits_past_delayed_connection_greeting(self):
        sock = FakeSocket()
        with (
            patch.object(app.socket, "create_connection", return_value=sock),
            patch.object(app, "recv_hyperdeck_message", side_effect=[
                "",
                "500 connection info:\r\nprotocol version: 1.12\r\n\r\n",
                "211 configuration:\r\ntimecode input: external\r\nvideo input: sdi\r\n\r\n",
            ]),
        ):
            info = app.get_hyperdeck_configuration_info({"ip": "10.0.0.24"})

        self.assertEqual(info["timecode input"], "external")
        self.assertEqual(sock.sent, [b"configuration\r\n"])

    def test_configuration_retries_after_connection_failure(self):
        sock = FakeSocket()
        with (
            patch.object(app.socket, "create_connection", side_effect=[OSError("busy"), sock]) as connect,
            patch.object(app, "recv_hyperdeck_message", side_effect=[
                "500 connection info:\r\n\r\n",
                "211 configuration:\r\ntimecode input: embedded\r\n\r\n",
            ]),
            patch.object(app.time, "sleep"),
        ):
            info = app.get_hyperdeck_configuration_info({"ip": "10.0.0.11"})

        self.assertEqual(info["timecode input"], "embedded")
        self.assertEqual(connect.call_count, 2)

    def test_configuration_uses_last_successful_value_after_failures(self):
        ip = "10.0.0.12"
        with app.CONFIGURATION_INFO_LOCK:
            app.CONFIGURATION_INFO_CACHE[ip] = {"timecode input": "internal"}

        with (
            patch.object(app.socket, "create_connection", side_effect=OSError("offline")),
            patch.object(app.time, "sleep"),
        ):
            info = app.get_hyperdeck_configuration_info({"ip": ip})

        self.assertEqual(info["timecode input"], "internal")

    def test_timecode_input_write_is_verified(self):
        sock = FakeSocket()
        with (
            patch.object(app.socket, "create_connection", return_value=sock),
            patch.object(app, "recv_hyperdeck_message", side_effect=[
                "",
                "500 connection info:\r\n\r\n",
                "200 ok\r\n\r\n",
                "200 ok\r\n\r\n",
            ]),
            patch.object(
                app,
                "get_hyperdeck_configuration_info",
                return_value={"timecode input": "external"},
            ) as read_back,
            patch.object(app.time, "sleep"),
        ):
            ok, message = app.set_hyperdeck_timecode_input(
                {"ip": "10.0.0.11"},
                "external",
            )

        self.assertTrue(ok)
        self.assertIn("potvrzeno", message)
        self.assertEqual(
            sock.sent,
            [
                b"remote: override: true\r\n",
                b"configuration: timecode input: external\r\n",
            ],
        )
        read_back.assert_called_once_with(
            {"ip": "10.0.0.11"},
            allow_cached_fallback=False,
        )

    def test_timecode_input_write_retries_after_connection_failure(self):
        sock = FakeSocket()
        with (
            patch.object(app.socket, "create_connection", side_effect=[OSError("busy"), sock]) as connect,
            patch.object(app, "recv_hyperdeck_message", side_effect=[
                "500 connection info:\r\n\r\n",
                "200 ok\r\n\r\n",
                "200 ok\r\n\r\n",
            ]),
            patch.object(
                app,
                "get_hyperdeck_configuration_info",
                return_value={"timecode input": "embedded"},
            ),
            patch.object(app.time, "sleep"),
        ):
            ok, _message = app.set_hyperdeck_timecode_input(
                {"ip": "10.0.0.12"},
                "embedded",
            )

        self.assertTrue(ok)
        self.assertEqual(connect.call_count, 2)

    def test_transport_status_skips_delayed_connection_greeting(self):
        sock = FakeSocket()
        with (
            patch.object(app.socket, "create_connection", return_value=sock),
            patch.object(app, "recv_hyperdeck_message", side_effect=[
                "",
                "500 connection info:\r\n\r\n",
                "208 transport info:\r\nstatus: record\r\n\r\n",
            ]),
        ):
            status = app.get_hyperdeck_transport_status({"ip": "10.0.0.11"})

        self.assertEqual(status, "record")
        self.assertEqual(sock.sent, [b"transport info\r\n"])

    def test_record_command_is_confirmed_by_transport_state(self):
        sock = FakeSocket()
        with (
            patch.object(app.socket, "create_connection", return_value=sock),
            patch.object(app, "recv_hyperdeck_message", side_effect=[
                "",
                "500 connection info:\r\n\r\n",
                "200 ok\r\n\r\n",
                "200 ok\r\n\r\n",
            ]),
            patch.object(app, "get_hyperdeck_transport_status", return_value="record") as verify,
            patch.object(app.time, "sleep"),
        ):
            ok, message = app.send_hyperdeck_command(
                {"ip": "10.0.0.11"},
                "rec",
            )

        self.assertTrue(ok)
        self.assertIn("potvrzeno", message)
        self.assertEqual(
            sock.sent,
            [b"remote: override: true\r\n", b"record\r\n"],
        )
        verify.assert_called_once()

    def test_record_command_retries_only_until_state_is_confirmed(self):
        first_sock = FakeSocket()
        second_sock = FakeSocket()
        with (
            patch.object(
                app.socket,
                "create_connection",
                side_effect=[first_sock, second_sock],
            ) as connect,
            patch.object(app, "recv_hyperdeck_message", side_effect=[
                "500 connection info:\r\n\r\n",
                "200 ok\r\n\r\n",
                "200 ok\r\n\r\n",
                "500 connection info:\r\n\r\n",
                "200 ok\r\n\r\n",
                "200 ok\r\n\r\n",
            ]),
            patch.object(
                app,
                "get_hyperdeck_transport_status",
                side_effect=["stopped", "stopped", "recording"],
            ),
            patch.object(app.time, "sleep"),
        ):
            ok, _message = app.send_hyperdeck_command(
                {"ip": "10.0.0.12"},
                "rec",
            )

        self.assertTrue(ok)
        self.assertEqual(connect.call_count, 2)
        self.assertEqual(first_sock.sent[-1], b"record\r\n")
        self.assertEqual(second_sock.sent[-1], b"record\r\n")

    def test_parallel_commands_preserve_deck_order_and_failures(self):
        decks = [
            {"name": "Cam 1", "ip": "10.0.0.11"},
            {"name": "Cam 2", "ip": "10.0.0.12"},
        ]

        def command_result(deck, _cmd):
            if deck["name"] == "Cam 1":
                return True, "confirmed"
            return False, "not confirmed"

        with patch.object(app, "send_hyperdeck_command", side_effect=command_result):
            results = app.send_hyperdeck_commands_parallel(decks, "stop")

        self.assertEqual([item["name"] for item in results], ["Cam 1", "Cam 2"])
        self.assertEqual([item["ok"] for item in results], [True, False])

    def test_format_slots_share_connection_and_skip_delayed_greeting(self):
        sock = FakeSocket()
        with (
            patch.object(app.socket, "create_connection", return_value=sock) as connect,
            patch.object(app, "recv_hyperdeck_message", side_effect=[
                "",
                "500 connection info:\r\n\r\n",
                "211 configuration:\r\ndefault standard: 1080p50\r\n\r\n",
                "202 slot info:\r\nstatus: mounted\r\nrecording time: 125\r\n\r\n",
                "202 slot info:\r\nstatus: empty\r\n\r\n",
            ]),
        ):
            result = app.get_format_slots_status(
                "10.0.0.24",
                expected_format="1080p50",
                slot_ids=(1, 2),
            )

        self.assertEqual(result[1]["text"], "02:05")
        self.assertTrue(result[1]["valid"])
        self.assertEqual(result[2]["css_class"], "sd-empty")
        self.assertTrue(result[2]["valid"])
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(
            sock.sent,
            [
                b"configuration\r\n",
                b"slot info: slot id: 1\r\n",
                b"slot info: slot id: 2\r\n",
            ],
        )

    def test_format_slot_uses_cached_state_after_transient_failure(self):
        cache_key = ("10.0.0.24", "1080p50", 1)
        with app.FORMAT_SLOT_STATUS_LOCK:
            app.FORMAT_SLOT_STATUS_CACHE[cache_key] = ("45:08", "")

        with (
            patch.object(app.socket, "create_connection", side_effect=OSError("busy")),
            patch.object(app.time, "sleep"),
        ):
            result = app.get_format_slots_status(
                "10.0.0.24",
                expected_format="1080p50",
                slot_ids=(1,),
            )

        self.assertEqual(result[1]["text"], "45:08")
        self.assertTrue(result[1]["valid"])
        self.assertFalse(result[1]["fresh"])

    def test_unverified_format_slot_does_not_claim_media_is_empty(self):
        with (
            patch.object(app.socket, "create_connection", side_effect=OSError("busy")),
            patch.object(app.time, "sleep"),
        ):
            result = app.get_format_slots_status(
                "10.0.0.24",
                expected_format="1080p50",
                slot_ids=(1,),
            )

        self.assertFalse(result[1]["valid"])


if __name__ == "__main__":
    unittest.main()
