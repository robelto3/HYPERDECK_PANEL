import io
import unittest

import app


def make_handler(path, token=""):
    handler = object.__new__(app.Handler)
    handler.path = path
    handler.headers = {
        "Content-Length": "0",
        "X-CSRF-Token": token,
    }
    handler.rfile = io.BytesIO(b"")
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
            handler = make_handler("/api/format-hd-mini-slot1", app.CSRF_TOKEN)
            handler.handle_POST()
        finally:
            lock.release()

        self.assertEqual(handler.response_code, 409)
        self.assertIn("už probíhá", handler.wfile.getvalue().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
