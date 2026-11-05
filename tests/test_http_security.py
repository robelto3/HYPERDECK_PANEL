import io
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import app


def make_handler(path, token="", body=b""):
    handler = object.__new__(app.Handler)
    handler.path = path
    handler.headers = {
        "Content-Length": str(len(body)),
        "X-CSRF-Token": token,
    }
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler.response_code = None
    handler.response_headers = []
    handler.error_code = None
    handler.send_response = lambda code: setattr(handler, "response_code", code)
    handler.send_header = lambda name, value: handler.response_headers.append((name, value))
    handler.end_headers = lambda: None
    handler.send_error = lambda code, *args: setattr(handler, "error_code", code)
    return handler


class CsrfTests(unittest.TestCase):
    def test_api_post_without_token_is_forbidden(self):
        handler = make_handler("/api/close-panel")
        handler.handle_POST()
        self.assertEqual(handler.response_code, 403)
        self.assertIn("bezpečnostní token", handler.wfile.getvalue().decode("utf-8"))

    def test_form_post_without_token_is_forbidden(self):
        handler = make_handler("/save-settings")
        handler.handle_POST()
        self.assertEqual(handler.error_code, 403)

    def test_valid_token_passes_csrf_check(self):
        handler = make_handler("/unknown", app.CSRF_TOKEN)
        handler.handle_POST()
        self.assertEqual(handler.error_code, 404)

    def test_valid_form_token_passes_csrf_check(self):
        body = f"csrf_token={app.CSRF_TOKEN}".encode("ascii")
        handler = make_handler("/unknown")
        handler.headers["Content-Length"] = str(len(body))
        handler.rfile = io.BytesIO(body)
        handler.handle_POST()
        self.assertEqual(handler.error_code, 404)


class FormatLockTests(unittest.TestCase):
    def test_format_devices_have_independent_locks(self):
        self.assertIsNot(
            app.FORMAT_LOCKS["format_ip_hd_mini"],
            app.FORMAT_LOCKS["format_ip_4k_pro"],
        )

    def test_second_format_request_returns_conflict(self):
        lock = app.FORMAT_LOCKS["format_ip_hd_mini"]
        lock.acquire()

        try:
            handler = make_handler(
                "/api/format-hd-mini",
                app.CSRF_TOKEN,
                body=b"slot=1",
            )
            handler.handle_POST()
        finally:
            lock.release()

        self.assertEqual(handler.response_code, 409)
        self.assertIn("už probíhá", handler.wfile.getvalue().decode("utf-8"))

    def test_format_request_requires_selected_slot(self):
        handler = make_handler("/api/format-hd-mini", app.CSRF_TOKEN)
        handler.handle_POST()
        self.assertEqual(handler.response_code, 400)


class SequentialFormatTests(unittest.TestCase):
    def test_selected_slots_are_formatted_in_order(self):
        with patch.object(app, "format_hyperdeck_slot", side_effect=[
            (True, "slot 1 ok"),
            (True, "slot 2 ok"),
        ]) as format_slot:
            ok, results = app.format_hyperdeck_slots("10.0.0.24", [1, 2])

        self.assertTrue(ok)
        self.assertEqual([item["slot"] for item in results], [1, 2])
        self.assertEqual(
            [call.kwargs["slot_id"] for call in format_slot.call_args_list],
            [1, 2],
        )

    def test_second_slot_is_skipped_after_failure(self):
        with patch.object(app, "format_hyperdeck_slot", return_value=(False, "failed")) as format_slot:
            ok, results = app.format_hyperdeck_slots("10.0.0.24", [1, 2])

        self.assertFalse(ok)
        self.assertEqual(len(results), 1)
        format_slot.assert_called_once()

    def test_status_payload_contains_both_slots(self):
        with (
            patch.object(app, "load_config", return_value={"expected_format": "1080p50"}),
            patch.object(app, "load_advanced_nvm", return_value={"format_ip_hd_mini": "10.0.0.24"}),
            patch.object(app, "get_format_slots_status", return_value={
                1: {"text": "01h00", "css_class": "", "valid": True, "fresh": True},
                2: {"text": "02h00", "css_class": "", "valid": True, "fresh": True},
            }) as slot_status,
        ):
            payload = app.build_format_slots_status_payload("format_ip_hd_mini")

        self.assertEqual(payload["slots"]["1"]["text"], "01h00")
        self.assertEqual(payload["slots"]["2"]["text"], "02h00")
        slot_status.assert_called_once_with(
            "10.0.0.24",
            expected_format="1080p50",
            slot_ids=(1, 2),
        )


class ActionCommandTests(unittest.TestCase):
    def test_rec_all_redirect_reports_unconfirmed_deck(self):
        body = f"csrf_token={app.CSRF_TOKEN}&cmd=rec_all".encode("ascii")
        handler = make_handler("/action", body=body)
        cfg = {"decks": [
            {"name": "Cam 1", "ip": "10.0.0.11", "armed": True},
            {"name": "Režie", "ip": "10.0.0.24", "armed": True},
        ]}
        results = [
            {"index": 0, "name": "Cam 1", "ip": "10.0.0.11", "ok": False, "response": "failed"},
            {"index": 1, "name": "Režie", "ip": "10.0.0.24", "ok": True, "response": "confirmed"},
        ]

        with (
            patch.object(app, "load_config", return_value=cfg),
            patch.object(app, "send_hyperdeck_commands_parallel", return_value=results),
            patch.object(app, "start_atem_logger_edl", return_value=(
                True,
                {"active": True, "file": "05112604.edl", "error": ""},
            )),
        ):
            handler.handle_POST()

        self.assertEqual(handler.response_code, 303)
        location = dict(handler.response_headers)["Location"]
        query = parse_qs(urlparse(location).query)
        self.assertEqual(query["action_result"], ["REC ALL potvrzeno: 1/2"])
        self.assertEqual(query["action_failed"], ["Cam 1"])

    def test_rec_all_success_redirect_has_no_green_confirmation(self):
        body = f"csrf_token={app.CSRF_TOKEN}&cmd=rec_all".encode("ascii")
        handler = make_handler("/action", body=body)
        cfg = {"decks": [
            {"name": "Režie", "ip": "10.0.0.24", "armed": True},
        ]}
        results = [
            {"index": 0, "name": "Režie", "ip": "10.0.0.24", "ok": True, "response": "confirmed"},
        ]

        with (
            patch.object(app, "load_config", return_value=cfg),
            patch.object(app, "send_hyperdeck_commands_parallel", return_value=results),
            patch.object(app, "start_atem_logger_edl", return_value=(
                True,
                {"active": True, "file": "05112604.edl", "error": ""},
            )),
        ):
            handler.handle_POST()

        self.assertEqual(handler.response_code, 303)
        self.assertEqual(dict(handler.response_headers)["Location"], "/")

    def test_rec_all_continues_and_reports_logger_failure(self):
        body = f"csrf_token={app.CSRF_TOKEN}&cmd=rec_all".encode("ascii")
        handler = make_handler("/action", body=body)
        cfg = {"decks": [
            {"name": "Režie", "ip": "10.0.0.24", "armed": True},
        ]}
        results = [
            {"index": 0, "name": "Režie", "ip": "10.0.0.24", "ok": True, "response": "confirmed"},
        ]

        with (
            patch.object(app, "load_config", return_value=cfg),
            patch.object(app, "send_hyperdeck_commands_parallel", return_value=results),
            patch.object(app, "start_atem_logger_edl", return_value=(
                False,
                {"active": False, "file": "", "error": "logger offline"},
            )) as start_edl,
        ):
            handler.handle_POST()

        start_edl.assert_called_once_with(cfg)
        location = dict(handler.response_headers)["Location"]
        query = parse_qs(urlparse(location).query)
        self.assertEqual(query["action_result"], ["REC ALL potvrzeno: 1/1"])
        self.assertEqual(query["action_failed"], ["EDL: logger offline"])

    def test_partial_stop_leaves_edl_active(self):
        body = f"csrf_token={app.CSRF_TOKEN}&cmd=stop_all".encode("ascii")
        handler = make_handler("/action", body=body)
        cfg = {"decks": [
            {"name": "Cam 1", "ip": "10.0.0.11", "armed": True},
            {"name": "Režie", "ip": "10.0.0.24", "armed": True},
        ]}
        results = [
            {"index": 0, "name": "Cam 1", "ip": "10.0.0.11", "ok": False, "response": "failed"},
            {"index": 1, "name": "Režie", "ip": "10.0.0.24", "ok": True, "response": "confirmed"},
        ]

        with (
            patch.object(app, "load_config", return_value=cfg),
            patch.object(app, "send_hyperdeck_commands_parallel", return_value=results),
            patch.object(app, "request_atem_logger_edl") as request_edl,
        ):
            handler.handle_POST()

        request_edl.assert_not_called()
        location = dict(handler.response_headers)["Location"]
        query = parse_qs(urlparse(location).query)
        self.assertEqual(query["action_result"], ["STOP ALL potvrzeno: 1/2"])
        self.assertEqual(query["action_failed"], ["Cam 1, EDL zůstal aktivní"])

    def test_confirmed_stop_closes_edl(self):
        body = f"csrf_token={app.CSRF_TOKEN}&cmd=stop_all".encode("ascii")
        handler = make_handler("/action", body=body)
        cfg = {"decks": [
            {"name": "Režie", "ip": "10.0.0.24", "armed": True},
        ]}
        results = [
            {"index": 0, "name": "Režie", "ip": "10.0.0.24", "ok": True, "response": "confirmed"},
        ]

        with (
            patch.object(app, "load_config", return_value=cfg),
            patch.object(app, "send_hyperdeck_commands_parallel", return_value=results),
            patch.object(app, "request_atem_logger_edl", return_value=(
                True,
                {"active": False, "last_closed_file": "05112604.edl", "error": ""},
            )) as request_edl,
        ):
            handler.handle_POST()

        request_edl.assert_called_once_with(cfg, "stop")
        self.assertEqual(dict(handler.response_headers)["Location"], "/")

    def test_save_settings_reports_unconfirmed_logger_fps(self):
        body = f"csrf_token={app.CSRF_TOKEN}".encode("ascii")
        handler = make_handler("/save-settings", body=body)
        cfg = {
            "atem_logger_ip": "10.0.0.9",
            "decks": [
                {"name": "Režie", "ip": "10.0.0.24", "armed": True},
            ],
        }
        nvm = {"deck_video_formats": ["1080p50"]}
        failed_status = {
            "available": True,
            "fps": 25,
            "error": "Logger hlásí 25 fps místo požadovaných 50 fps.",
        }

        with (
            patch.object(app, "load_config", return_value=cfg),
            patch.object(app, "load_advanced_nvm", return_value=nvm),
            patch.object(app, "stop_all_status_pollers"),
            patch.object(app, "save_config"),
            patch.object(app, "save_advanced_nvm"),
            patch.object(
                app,
                "synchronize_atem_video_format",
                return_value=(True, {"error": ""}, "1080p50"),
            ),
            patch.object(
                app,
                "synchronize_atem_logger_fps",
                return_value=(False, failed_status, 50),
            ) as synchronize_fps,
        ):
            handler.handle_POST()

        synchronize_fps.assert_called_once()
        location = dict(handler.response_headers)["Location"]
        query = parse_qs(urlparse(location).query)
        self.assertEqual(query["saved"], ["1"])
        self.assertIn("50 fps", query["logger_fps_failed"][0])

    def test_save_settings_reports_unconfirmed_atem_format(self):
        body = f"csrf_token={app.CSRF_TOKEN}".encode("ascii")
        handler = make_handler("/save-settings", body=body)
        cfg = {
            "atem_logger_ip": "10.0.0.9",
            "decks": [
                {"name": "Režie", "ip": "10.0.0.24", "armed": True},
            ],
        }
        nvm = {"deck_video_formats": ["1080i50"]}
        failed_status = {
            "available": True,
            "atem_video_format": "1080p50",
            "error": "ATEM hlásí 1080p50 místo 1080i50.",
        }

        with (
            patch.object(app, "load_config", return_value=cfg),
            patch.object(app, "load_advanced_nvm", return_value=nvm),
            patch.object(app, "stop_all_status_pollers"),
            patch.object(app, "save_config"),
            patch.object(app, "save_advanced_nvm"),
            patch.object(
                app,
                "synchronize_atem_video_format",
                return_value=(False, failed_status, "1080i50"),
            ) as synchronize_format,
            patch.object(
                app,
                "synchronize_atem_logger_fps",
                return_value=(True, {"error": ""}, 25),
            ),
        ):
            handler.handle_POST()

        synchronize_format.assert_called_once()
        location = dict(handler.response_headers)["Location"]
        query = parse_qs(urlparse(location).query)
        self.assertEqual(query["saved"], ["1"])
        self.assertIn("1080i50", query["atem_format_failed"][0])


class AtemLoggerTests(unittest.TestCase):
    def test_logger_state_keeps_confirmed_filename(self):
        status = app.normalize_atem_logger_status({
            "atem": True,
            "atem_product": "ATEM Television Studio Pro 4K",
            "atem_video_mode_valid": True,
            "atem_video_mode": 12,
            "atem_video_format": "1080p50",
            "atem_video_modes_valid": True,
            "atem_supported_video_modes": 1048528,
            "edl_active": True,
            "edl_state": "active",
            "edl_generation": 4,
            "edl_file": "05112604.edl",
            "edl_last_closed_file": "05112603.edl",
            "edl_error": "",
            "tc_out_fps": 50,
        }, ip="10.0.0.9")

        self.assertTrue(status["available"])
        self.assertTrue(status["active"])
        self.assertEqual(status["file"], "05112604.edl")
        self.assertEqual(status["generation"], 4)
        self.assertEqual(status["fps"], 50)
        self.assertTrue(status["atem_connected"])
        self.assertEqual(status["atem_product"], "ATEM Television Studio Pro 4K")
        self.assertEqual(status["atem_video_mode"], 12)
        self.assertEqual(status["atem_video_format"], "1080p50")

    def test_start_requires_active_filename_confirmation(self):
        cfg = {"atem_logger_ip": "10.0.0.9"}
        payload = {
            "ok": True,
            "edl_active": True,
            "edl_state": "active",
            "edl_generation": 1,
            "edl_file": "05112604.edl",
            "edl_last_closed_file": "",
            "error": "",
        }

        with patch.object(app, "atem_logger_http_json", return_value=(True, payload, "")):
            ok, status = app.request_atem_logger_edl(cfg, "start")

        self.assertTrue(ok)
        self.assertEqual(status["file"], "05112604.edl")

    def test_control_room_format_selects_logger_fps(self):
        cfg = {"decks": [
            {"name": "Cam 1"},
            {"name": "Režie"},
        ]}

        cases = {
            "1080p25": 25,
            "1080p50": 50,
            "1080i50": 25,
            "uhdp25": 25,
            "uhdp50": 50,
            "4kp25": 25,
        }

        for video_format, expected_fps in cases.items():
            with self.subTest(video_format=video_format):
                nvm = {"deck_video_formats": ["1080p50", video_format]}
                self.assertEqual(
                    app.get_control_room_expected_fps(cfg, nvm=nvm),
                    expected_fps,
                )

    def test_control_room_format_selects_atem_output_format(self):
        cfg = {"decks": [
            {"name": "Cam 1"},
            {"name": "Režie"},
        ]}

        for video_format in app.VIDEO_FORMAT_VALUES:
            with self.subTest(video_format=video_format):
                nvm = {"deck_video_formats": ["1080p50", video_format]}
                self.assertEqual(
                    app.get_control_room_expected_format(cfg, nvm=nvm),
                    video_format,
                )

    def test_atem_format_synchronization_uses_control_room_format(self):
        cfg = {"decks": [{"name": "Režie"}]}
        nvm = {"deck_video_formats": ["1080i50"]}
        confirmed_status = {
            "available": True,
            "atem_connected": True,
            "atem_video_mode_valid": True,
            "atem_video_format": "1080i50",
            "error": "",
        }

        with patch.object(
            app,
            "set_atem_logger_video_format",
            return_value=(True, confirmed_status),
        ) as set_format:
            ok, status, expected_format = app.synchronize_atem_video_format(
                cfg,
                nvm=nvm,
            )

        set_format.assert_called_once_with(cfg, "1080i50")
        self.assertTrue(ok)
        self.assertTrue(status["atem_format_matches"])
        self.assertEqual(expected_format, "1080i50")

    def test_atem_format_write_requires_exact_confirmation(self):
        cfg = {"atem_logger_ip": "10.0.0.9"}
        current_status = {
            "ip": "10.0.0.9",
            "available": True,
            "atem_connected": True,
            "atem_product": "ATEM Television Studio Pro 4K",
            "atem_video_mode_valid": True,
            "atem_video_mode": 12,
            "atem_video_format": "1080p50",
            "atem_video_modes_valid": True,
            "atem_supported_video_modes": 1048528,
            "active": False,
            "fps": 50,
            "error": "",
        }
        payload = {
            "ok": True,
            "atem": True,
            "atem_video_mode_valid": True,
            "atem_video_mode": 6,
            "atem_video_format": "1080i50",
            "error": "",
        }

        with (
            patch.object(app, "get_atem_logger_status", return_value=current_status),
            patch.object(
                app,
                "atem_logger_http_json",
                return_value=(True, payload, ""),
            ) as http_json,
        ):
            ok, status = app.set_atem_logger_video_format(cfg, "1080i50")

        self.assertTrue(ok)
        self.assertEqual(status["atem_video_format"], "1080i50")
        http_json.assert_called_once_with(
            "10.0.0.9",
            "/api/atem/video-mode?format=1080i50",
            method="POST",
            timeout=app.ATEM_LOGGER_VIDEO_MODE_TIMEOUT,
        )

    def test_active_edl_blocks_atem_format_change(self):
        cfg = {"atem_logger_ip": "10.0.0.9"}
        active_status = {
            "available": True,
            "atem_connected": True,
            "atem_video_mode_valid": True,
            "atem_video_format": "1080p50",
            "active": True,
            "error": "",
        }

        with (
            patch.object(app, "get_atem_logger_status", return_value=active_status),
            patch.object(app, "atem_logger_http_json") as http_json,
        ):
            ok, status = app.set_atem_logger_video_format(cfg, "1080i50")

        self.assertFalse(ok)
        self.assertIn("EDL je aktivní", status["error"])
        http_json.assert_not_called()

    def test_dci_format_is_not_sent_to_atem(self):
        cfg = {"atem_logger_ip": "10.0.0.9"}
        current_status = {
            "available": True,
            "atem_connected": True,
            "atem_video_mode_valid": True,
            "atem_video_format": "uhdp25",
            "active": False,
            "error": "",
        }

        with (
            patch.object(app, "get_atem_logger_status", return_value=current_status),
            patch.object(app, "atem_logger_http_json") as http_json,
        ):
            ok, status = app.set_atem_logger_video_format(cfg, "4kp25")

        self.assertFalse(ok)
        self.assertIn("DCI 4Kp25", status["error"])
        http_json.assert_not_called()

    def test_fps_synchronization_uses_control_room_format(self):
        cfg = {"decks": [{"name": "Režie"}]}
        nvm = {"deck_video_formats": ["uhdp25"]}
        confirmed_status = {
            "available": True,
            "fps": 25,
            "error": "",
        }

        with patch.object(
            app,
            "set_atem_logger_tc_out_fps",
            return_value=(True, confirmed_status),
        ) as set_fps:
            ok, status, expected_fps = app.synchronize_atem_logger_fps(
                cfg,
                nvm=nvm,
            )

        set_fps.assert_called_once_with(cfg, 25)
        self.assertTrue(ok)
        self.assertTrue(status["fps_matches"])
        self.assertEqual(expected_fps, 25)

    def test_active_edl_blocks_fps_change(self):
        cfg = {"atem_logger_ip": "10.0.0.9"}
        active_status = {
            "available": True,
            "active": True,
            "fps": 50,
            "error": "",
        }

        with (
            patch.object(
                app,
                "get_atem_logger_status",
                return_value=active_status,
            ),
            patch.object(app.http.client, "HTTPConnection") as connection,
        ):
            ok, status = app.set_atem_logger_tc_out_fps(cfg, 25)

        self.assertFalse(ok)
        self.assertIn("EDL je aktivní", status["error"])
        connection.assert_not_called()

    def test_edl_is_not_started_when_fps_is_unconfirmed(self):
        cfg = {"decks": [{"name": "Režie"}]}
        failed_status = {
            "available": True,
            "fps": 50,
            "error": "Logger hlásí 50 fps místo požadovaných 25 fps.",
        }

        with (
            patch.object(
                app,
                "synchronize_atem_video_format",
                return_value=(True, {"error": ""}, "1080p25"),
            ),
            patch.object(
                app,
                "synchronize_atem_logger_fps",
                return_value=(False, failed_status, 25),
            ),
            patch.object(app, "request_atem_logger_edl") as request_edl,
        ):
            ok, status = app.start_atem_logger_edl(cfg)

        self.assertFalse(ok)
        self.assertEqual(status["fps"], 50)
        request_edl.assert_not_called()

    def test_panel_status_warns_about_fps_mismatch(self):
        cfg = {"decks": [{"name": "Režie"}]}
        logger_status = {
            "available": True,
            "atem_connected": True,
            "atem_video_mode_valid": True,
            "atem_video_format": "1080p50",
            "active": False,
            "state": "idle",
            "fps": 25,
            "file": "",
            "last_closed_file": "",
            "error": "",
        }

        with (
            patch.object(app, "get_atem_logger_status", return_value=logger_status),
            patch.object(app, "get_control_room_expected_format", return_value="1080p50"),
            patch.object(app, "get_control_room_expected_fps", return_value=50),
        ):
            status = app.build_atem_logger_panel_status(cfg)

        self.assertFalse(status["fps_matches"])
        self.assertIn("Režie vyžaduje 50 fps", status["error"])

    def test_panel_status_warns_about_atem_format_mismatch(self):
        cfg = {"decks": [{"name": "Režie"}]}
        logger_status = {
            "available": True,
            "atem_connected": True,
            "atem_video_mode_valid": True,
            "atem_video_format": "1080p50",
            "active": False,
            "state": "idle",
            "fps": 25,
            "file": "",
            "last_closed_file": "",
            "error": "",
        }

        with (
            patch.object(app, "get_atem_logger_status", return_value=logger_status),
            patch.object(app, "get_control_room_expected_format", return_value="1080i50"),
            patch.object(app, "get_control_room_expected_fps", return_value=25),
        ):
            status = app.build_atem_logger_panel_status(cfg)

        self.assertFalse(status["atem_format_matches"])
        self.assertIn("ATEM má 1080p50", status["error"])


if __name__ == "__main__":
    unittest.main()
