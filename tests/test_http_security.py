import io
import unittest
from unittest.mock import patch

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
            patch.object(app, "get_format_slot_status", side_effect=[
                ("01h00", ""),
                ("02h00", ""),
            ]) as slot_status,
        ):
            payload = app.build_format_slots_status_payload("format_ip_hd_mini")

        self.assertEqual(payload["slots"]["1"]["text"], "01h00")
        self.assertEqual(payload["slots"]["2"]["text"], "02h00")
        self.assertEqual(
            [call.kwargs["slot_id"] for call in slot_status.call_args_list],
            [1, 2],
        )


if __name__ == "__main__":
    unittest.main()
