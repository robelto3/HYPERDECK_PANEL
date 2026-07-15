import unittest

import app


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


if __name__ == "__main__":
    unittest.main()
