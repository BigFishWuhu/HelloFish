import base64
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "cloud"))
from server import CloudServer  # noqa: E402


class CloudServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.server = CloudServer(
            ("127.0.0.1", 0),
            data_root=root / "data",
            web_root=Path(__file__).resolve().parents[1] / "cloud" / "web" / "contributions",
            initial_users={"alice": "secret"},
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def call(self, path: str, data=None, headers=None, method=None):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(data).encode("utf-8") if data is not None else None,
            headers=headers or {},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as error:
            try:
                return error.code, error.headers, error.read()
            finally:
                error.close()

    def test_login_and_basic_upload_are_authenticated(self) -> None:
        self.assertEqual(self.call("/api/records")[0], 401)
        credentials = base64.b64encode(b"alice:secret").decode("ascii")
        headers = {"Authorization": f"Basic {credentials}", "Content-Type": "application/json"}
        status, _, body = self.call(
            "/api/records/upload",
            {"records": [{"room_id": "100", "user_id": "u1", "scanned_at": "2026-09-16T00:00:00+08:00"}]},
            headers,
            "POST",
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["saved"], 1)
        self.assertEqual(self.call("/api/records", headers=headers)[0], 200)

    def test_static_assets_disable_caching_and_version_the_app_script(self) -> None:
        status, headers, body = self.call("/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "no-store, no-cache, must-revalidate, max-age=0")
        self.assertIn(b"/styles.css?v=", body)
        self.assertIn(b"/app.js?v=", body)

    def test_authenticated_user_can_export_static_html(self) -> None:
        credentials = base64.b64encode(b"alice:secret").decode("ascii")
        headers = {"Authorization": f"Basic {credentials}"}
        status, response_headers, body = self.call(
            "/api/export.html?date_mode=custom&start_date=2026-09-14&end_date=2026-09-14",
            headers=headers,
        )

        self.assertEqual(status, 200)
        self.assertEqual(response_headers.get_content_type(), "text/html")
        self.assertIn(".html", response_headers["Content-Disposition"])
        self.assertIn(
            "script-src 'unsafe-inline'",
            response_headers["Content-Security-Policy"],
        )
        self.assertIn("贡献记录", body.decode("utf-8"))

    def test_authenticated_html_export_keeps_view_layout_with_legacy_columns(self) -> None:
        credentials = base64.b64encode(b"alice:secret").decode("ascii")
        headers = {"Authorization": f"Basic {credentials}"}
        status, _, body = self.call(
            "/api/export.html?date_mode=custom&start_date=2026-09-14&end_date=2026-09-14"
            "&columns=username,room_name",
            headers=headers,
        )

        self.assertEqual(status, 200)
        document = body.decode("utf-8")
        self.assertIn("<th>用户名</th><th>出现厅</th><th>最近记录</th>", document)

    def test_first_user_setup_is_one_time(self) -> None:
        self.server.users.clear()
        token = self.server.setup_first_user("first-user", "secret-1")
        self.assertTrue(token)
        self.assertEqual(self.server.authenticate("first-user", "secret-1") is not None, True)
        self.assertIsNone(self.server.setup_first_user("second-user", "secret-2"))
        self.assertNotIn("second-user", self.server.users)

    def test_change_password_requires_current_password_and_rotates_session(self) -> None:
        credentials = base64.b64encode(b"alice:secret").decode("ascii")
        auth_headers = {"Authorization": f"Basic {credentials}"}
        status, response_headers, _ = self.call(
            "/api/auth/change-password",
            {
                "current_password": "secret",
                "new_password": "new-secret",
                "password_confirmation": "new-secret",
            },
            {**auth_headers, "Content-Type": "application/json"},
            "POST",
        )
        self.assertEqual(status, 200)
        self.assertTrue(response_headers.get("Set-Cookie"))
        self.assertIsNone(self.server.authenticate("alice", "secret"))
        self.assertIsNotNone(self.server.authenticate("alice", "new-secret"))

    def test_change_password_rejects_wrong_current_password_and_mismatch(self) -> None:
        credentials = base64.b64encode(b"alice:secret").decode("ascii")
        headers = {"Authorization": f"Basic {credentials}", "Content-Type": "application/json"}
        status, _, body = self.call(
            "/api/auth/change-password",
            {
                "current_password": "wrong",
                "new_password": "new-secret",
                "password_confirmation": "new-secret",
            },
            headers,
            "POST",
        )
        self.assertEqual(status, 401)
        self.assertIn("当前密码错误", json.loads(body)["error"])

        status, _, body = self.call(
            "/api/auth/change-password",
            {
                "current_password": "secret",
                "new_password": "new-secret",
                "password_confirmation": "different",
            },
            headers,
            "POST",
        )
        self.assertEqual(status, 400)
        self.assertIn("不一致", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
