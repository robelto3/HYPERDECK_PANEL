import copy
import unittest
from unittest.mock import patch

import app


def state(frames, valid=True):
    return ("STOPPED", "stopped", "1080p50", app.frames_to_tcx2(frames), valid, frames)


class ConfigTests(unittest.TestCase):
    def test_ipv4_normalization(self):
        self.assertEqual(app.normalize_ipv4(" 10.0.0.11 "), "10.0.0.11")
        self.assertEqual(app.normalize_ipv4("not-an-ip", "10.0.0.1"), "10.0.0.1")
        self.assertEqual(app.normalize_ipv4("", allow_empty=True), "")
        self.assertEqual(app.normalize_ipv4("::1", "10.0.0.1"), "10.0.0.1")

    def test_video_format_choices_are_limited(self):
        self.assertEqual(app.normalize_video_format_choice("2160P50"), "2160p50")
        self.assertEqual(app.normalize_video_format_choice("720p50"), "1080p50")

    def test_missing_decks_are_inserted_before_control_room(self):
        cfg = {"decks": [{"name": "Cam 1", "ip": "10.0.0.11"}, {"name": "Režie", "ip": "10.0.0.24"}]}
        app.normalize_deck_list(cfg)
        self.assertEqual(len(cfg["decks"]), len(app.DEFAULT_CONFIG["decks"]))
        self.assertEqual(cfg["decks"][-1]["name"], "Režie")

    def test_control_room_is_always_armed(self):
        cfg = {"decks": [{"name": "Cam", "armed": False}, {"name": "Režie", "armed": False}]}
        self.assertFalse(app.deck_is_armed_at_index(cfg, 0, cfg["decks"][0]))
        self.assertTrue(app.deck_is_armed_at_index(cfg, 1, cfg["decks"][1]))

    def test_normalization_does_not_modify_defaults(self):
        original = copy.deepcopy(app.DEFAULT_CONFIG)
        cfg = {"decks": []}
        app.normalize_deck_list(cfg)
        cfg["decks"][0]["name"] = "Changed"
        self.assertEqual(app.DEFAULT_CONFIG, original)

    def test_network_interfaces_have_operational_labels(self):
        with patch.object(app, "get_system_network_interfaces", return_value=[
            ("eth0", "10.0.0.8"),
            ("eth1", "10.0.1.8"),
        ]):
            output = app.make_network_interfaces_html()

        self.assertIn("eth0 -&gt; Recorders", output)
        self.assertIn("eth1 -&gt; Atem Logger", output)


class TimecodeComparisonTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "tc_compare": True,
            "tc_tolerance_frames": 2,
            "tc_reference_mode": "group",
            "decks": [
                {"name": "Cam 1", "armed": True},
                {"name": "Cam 2", "armed": True},
                {"name": "Režie", "armed": True},
            ],
        }

    def test_values_inside_tolerance_are_ok(self):
        result = app.build_tc_compare(self.cfg, {0: state(100), 1: state(102), 2: state(101)})
        self.assertEqual(result["bad"], [])

    def test_outlier_is_reported(self):
        result = app.build_tc_compare(self.cfg, {0: state(100), 1: state(101), 2: state(110)})
        self.assertEqual([item["name"] for item in result["bad"]], ["Režie"])

    def test_control_room_can_be_reference(self):
        self.cfg["tc_reference_mode"] = "control_room"
        result = app.build_tc_compare(self.cfg, {0: state(100), 1: state(101), 2: state(110)})
        self.assertEqual(result["reference"]["name"], "Režie")
        self.assertEqual({item["name"] for item in result["bad"]}, {"Cam 1", "Cam 2"})

    def test_invalid_control_room_reference_is_reported(self):
        self.cfg["tc_reference_mode"] = "control_room"
        result = app.build_tc_compare(self.cfg, {0: state(100), 1: state(101), 2: state(0, valid=False)})
        self.assertTrue(result["reference_invalid"])

    def test_disabled_comparison_has_no_reference(self):
        self.cfg["tc_compare"] = False
        result = app.build_tc_compare(self.cfg, {0: state(100), 1: state(200), 2: state(300)})
        self.assertFalse(result["enabled"])
        self.assertIsNone(result["reference"])


if __name__ == "__main__":
    unittest.main()
