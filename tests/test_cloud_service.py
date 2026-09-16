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

    def test_first_user_setup_is_one_time(self) -> None:
        self.server.users.clear()
        token = self.server.setup_first_user("first-user", "secret-1")
        self.assertTrue(token)
        self.assertEqual(self.server.authenticate("first-user", "secret-1") is not None, True)
        self.assertIsNone(self.server.setup_first_user("second-user", "secret-2"))
        self.assertNotIn("second-user", self.server.users)


if __name__ == "__main__":
    unittest.main()
