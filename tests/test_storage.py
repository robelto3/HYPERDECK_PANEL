import unittest

import app


class StorageTests(unittest.TestCase):
    def test_recording_time_formats_minutes_hours_and_days(self):
        self.assertEqual(app.format_recording_time(125), "02:05")
        self.assertEqual(app.format_recording_time(3 * 3600 + 5 * 60), "3h05")
        self.assertEqual(app.format_recording_time(25 * 3600), "1d 01h")

    def test_resolution_change_scales_remaining_time(self):
        self.assertEqual(app.scale_recording_seconds(400, "1080p50", "2160p50"), 100)
        self.assertEqual(app.scale_recording_seconds(100, "2160p50", "1080p50"), 400)
        self.assertEqual(app.scale_recording_seconds(100, "1080p50", "1080p25"), 200)
        self.assertEqual(app.scale_recording_seconds(200, "1080p50", "2160p25"), 100)

    def test_unknown_format_does_not_scale_time(self):
        self.assertEqual(app.scale_recording_seconds(120, "unknown", "1080p50"), 120)

    def test_mounted_slot_reports_remaining_time(self):
        response = (
            "202 slot info:\r\nstatus: mounted\r\n"
            "recording time: 125\r\nvideo format: 1080p50\r\n\r\n"
        )
        self.assertEqual(
            app.slot_response_to_remaining_time(response, "1080p50", "1080p50"),
            ("02:05", ""),
        )

    def test_unmounted_slot_is_empty(self):
        response = "202 slot info:\r\nstatus: empty\r\n\r\n"
        self.assertEqual(app.slot_response_to_remaining_time(response, "1080p50", "1080p50"), ("---", "sd-empty"))

    def test_zero_recording_time_is_full(self):
        response = "202 slot info:\r\nstatus: mounted\r\nrecording time: 0\r\nremaining size: 0\r\n\r\n"
        self.assertEqual(app.slot_response_to_remaining_time(response, "1080p50", "1080p50"), ("Full", "sd-full"))

    def test_locked_slot_is_warning(self):
        response = "202 slot info:\r\nstatus: mounted\r\nblocked: true\r\n\r\n"
        self.assertEqual(app.slot_response_to_remaining_time(response, "1080p50", "1080p50"), ("Lock", "sd-warning"))

    def test_bad_slot_response_is_empty(self):
        self.assertEqual(app.slot_response_to_remaining_time("500 error", "1080p50", "1080p50"), ("---", "sd-empty"))


if __name__ == "__main__":
    unittest.main()
