"""Standalone authenticated HelloFish cloud viewer.

The service uses one SQLite database per account.  It is intentionally built
on the Python standard library so it can be deployed beside the existing
desktop agent without adding a framework runtime.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import threading
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from contribution_viewer import (  # noqa: E402
    RecordQuery,
    export_records_csv,
    export_records_html,
    load_settings,
    parse_export_columns,
    query_records,
    save_settings,
)
from voice_hall_storage import VoiceHallDatabase  # noqa: E402


ROOT = Path(__file__).resolve().parent
DEFAULT_DATA = ROOT / "data"
DEFAULT_WEB = ROOT / "web" / "contributions"
SESSION_COOKIE = "hellofish_cloud_session"
PBKDF2_ROUNDS = 210_000


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"{base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def _password_matches(password: str, encoded: str) -> bool:
    try:
        salt_text, digest_text = encoded.split("$", 1)
        salt = base64.urlsafe_b64decode(salt_text.encode())
        expected = base64.urlsafe_b64decode(digest_text.encode())
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return hmac.compare_digest(actual, expected)


class CloudServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        data_root: Path = DEFAULT_DATA,
        web_root: Path = DEFAULT_WEB,
        users_path: Path | None = None,
        initial_users: dict[str, str] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.web_root = Path(web_root)
        self.users_path = Path(users_path or self.data_root / "users.json")
        self._sessions: dict[str, str] = {}
        self._lock = threading.RLock()
        self.users = self._load_users()
        if initial_users:
            for username, password in initial_users.items():
                self.users.setdefault(username, _password_hash(password))
            self._save_users()
        self.data_root.mkdir(parents=True, exist_ok=True)
        super().__init__(address, CloudHandler)

    def _load_users(self) -> dict[str, str]:
        try:
            value = json.loads(self.users_path.read_text(encoding="utf-8"))
            if isinstance(value, dict):
                return {str(k): str(v) for k, v in value.items() if str(k).strip() and isinstance(v, str)}
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass
        return {}

    def _save_users(self) -> None:
        self.users_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.users_path.with_suffix(self.users_path.suffix + ".tmp")
        temp.write_text(json.dumps(self.users, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.users_path)

    def authenticate(self, username: str, password: str) -> str | None:
        encoded = self.users.get(username)
        if not encoded or not _password_matches(password, encoded):
            return None
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = username
        return token

    def setup_first_user(self, username: str, password: str) -> str | None:
        """Create the first account exactly once and return a session token."""
        username = username.strip()
        if not username or len(username) > 64:
            raise ValueError("账号长度必须为 1 到 64 个字符")
        if len(password) < 6:
            raise ValueError("密码至少需要 6 个字符")
        with self._lock:
            if self.users:
                return None
            self.users[username] = _password_hash(password)
            try:
                self._save_users()
            except OSError:
                self.users.pop(username, None)
                raise
            token = secrets.token_urlsafe(32)
            self._sessions[token] = username
            return token

    def change_password(self, username: str, current_password: str, new_password: str) -> str:
        """Change an existing user's password and return a fresh session token."""
        if len(new_password) < 6:
            raise ValueError("密码至少需要 6 个字符")
        with self._lock:
            encoded = self.users.get(username)
            if not encoded or not _password_matches(current_password, encoded):
                raise PermissionError("当前密码错误")
            previous = encoded
            self.users[username] = _password_hash(new_password)
            try:
                self._save_users()
            except OSError:
                self.users[username] = previous
                raise

            for token, session_user in list(self._sessions.items()):
                if session_user == username:
                    self._sessions.pop(token, None)
            token = secrets.token_urlsafe(32)
            self._sessions[token] = username
            return token

    def user_for_request(self, handler: BaseHTTPRequestHandler) -> str | None:
        authorization = handler.headers.get("Authorization", "")
        if authorization.lower().startswith("basic "):
            try:
                value = base64.b64decode(authorization[6:]).decode("utf-8")
                username, password = value.split(":", 1)
            except (ValueError, UnicodeDecodeError):
                return None
            if username in self.users and _password_matches(password, self.users[username]):
                return username
            return None
        cookie = SimpleCookie()
        cookie.load(handler.headers.get("Cookie", ""))
        token = cookie.get(SESSION_COOKIE)
        if token is None:
            return None
        with self._lock:
            return self._sessions.get(token.value)

    def database_for(self, username: str) -> tuple[Path, Path]:
        safe_name = "".join(ch for ch in username if ch.isalnum() or ch in "._-")[:60] or "user"
        safe_name += "-" + hashlib.sha256(username.encode("utf-8")).hexdigest()[:12]
        root = self.data_root / "accounts"
        root.mkdir(parents=True, exist_ok=True)
        database = root / f"{safe_name}.sqlite3"
        settings = root / f"{safe_name}.settings.json"
        VoiceHallDatabase(database).initialize()
        return database, settings


class CloudHandler(BaseHTTPRequestHandler):
    server: CloudServer

    def log_message(self, format: str, *args: Any) -> None:
        print("[HelloFishCloud] " + format % args)

    def _json(self, status: HTTPStatus, value: Any, headers: dict[str, str] | None = None) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {"error": message})

    def _body(self, limit: int = 2 * 1024 * 1024) -> dict[str, Any] | None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > limit:
                return None
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            return value if isinstance(value, dict) else None
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _auth(self) -> str | None:
        user = self.server.user_for_request(self)
        if user is None:
            self._error(HTTPStatus.UNAUTHORIZED, "请先登录")
        return user

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/auth/setup":
            value = self._body(64 * 1024)
            username = str(value.get("username", "")).strip() if value else ""
            password = str(value.get("password", "")) if value else ""
            confirmation = str(value.get("password_confirmation", "")) if value else ""
            if password != confirmation:
                self._error(HTTPStatus.BAD_REQUEST, "两次输入的密码不一致")
                return
            try:
                token = self.server.setup_first_user(username, password)
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except OSError as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"保存账号失败：{exc}")
                return
            if token is None:
                self._error(HTTPStatus.CONFLICT, "账号已初始化，请直接登录")
                return
            self._json(
                HTTPStatus.CREATED,
                {"username": username},
                {"Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"},
            )
            return
        if path == "/api/auth/login":
            value = self._body(64 * 1024)
            username = str(value.get("username", "")).strip() if value else ""
            password = str(value.get("password", "")) if value else ""
            token = self.server.authenticate(username, password)
            if token is None:
                self._error(HTTPStatus.UNAUTHORIZED, "账号或密码错误")
                return
            self._json(
                HTTPStatus.OK,
                {"username": username},
                {"Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"},
            )
            return
        if path == "/api/auth/change-password":
            username = self._auth()
            if username is None:
                return
            value = self._body(64 * 1024)
            current_password = str(value.get("current_password", "")) if value else ""
            new_password = str(value.get("new_password", "")) if value else ""
            confirmation = str(value.get("password_confirmation", "")) if value else ""
            if new_password != confirmation:
                self._error(HTTPStatus.BAD_REQUEST, "两次输入的新密码不一致")
                return
            try:
                token = self.server.change_password(username, current_password, new_password)
            except PermissionError as exc:
                self._error(HTTPStatus.UNAUTHORIZED, str(exc))
                return
            except ValueError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except OSError as exc:
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"保存密码失败：{exc}")
                return
            self._json(
                HTTPStatus.OK,
                {"username": username},
                {"Set-Cookie": f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax"},
            )
            return
        if path == "/api/auth/logout":
            cookie = SimpleCookie(); cookie.load(self.headers.get("Cookie", ""))
            token = cookie.get(SESSION_COOKIE)
            if token:
                self.server._sessions.pop(token.value, None)
            self._json(HTTPStatus.OK, {}, {"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly"})
            return
        if path != "/api/records/upload":
            self._error(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        username = self._auth()
        if username is None:
            return
        value = self._body()
        records = value.get("records") if value else None
        if isinstance(value, dict) and isinstance(value.get("record"), dict):
            records = [value["record"]]
        if not isinstance(records, list) or not records or len(records) > 500:
            self._error(HTTPStatus.BAD_REQUEST, "records 必须是 1 到 500 条记录")
            return
        database_path, _ = self.server.database_for(username)
        database = VoiceHallDatabase(database_path)
        saved = 0
        try:
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError("记录格式错误")
                database.upsert_contribution(record)
                saved += 1
        except (ValueError, sqlite3.Error, OSError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._json(HTTPStatus.OK, {"saved": saved})

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"service": "HelloFishCloud"})
            return
        if path == "/api/auth/me":
            username = self.server.user_for_request(self)
            self._json(
                HTTPStatus.OK,
                {
                    "authenticated": username is not None,
                    "username": username,
                    "setup_required": not bool(self.server.users),
                },
            )
            return
        static = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/styles.css": "styles.css"}.get(path)
        if static:
            payload = (self.server.web_root / static).read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "styles.css": "text/css; charset=utf-8"}[static])
            self.send_header("Content-Length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        username = self._auth()
        if username is None:
            return
        database_path, settings_path = self.server.database_for(username)
        parsed = urlparse(self.path)
        raw = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == "/api/records":
                self._json(HTTPStatus.OK, query_records(database_path, settings_path, RecordQuery.from_query(raw)))
                return
            if path == "/api/settings":
                self._json(HTTPStatus.OK, load_settings(settings_path))
                return
            if path == "/api/export":
                query = RecordQuery.from_query(raw)
                columns = parse_export_columns(raw)
                payload = export_records_csv(database_path, settings_path, query, columns)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition", 'attachment; filename="HelloFish-contributions.csv"')
                self.end_headers(); self.wfile.write(payload)
                return
            if path == "/api/export.html":
                query = RecordQuery.from_query(raw)
                payload = export_records_html(database_path, settings_path, query)
                filename = f"HelloFish-contributions-{query.start_date}-{query.end_date}.html"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
                )
                self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(payload)
                return
            self._error(HTTPStatus.NOT_FOUND, "页面不存在")
        except (OSError, sqlite3.Error, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_PUT(self) -> None:  # noqa: N802
        if urlparse(self.path).path != "/api/settings":
            self._error(HTTPStatus.NOT_FOUND, "接口不存在"); return
        username = self._auth()
        if username is None: return
        value = self._body(64 * 1024)
        if value is None:
            self._error(HTTPStatus.BAD_REQUEST, "需要 JSON 对象"); return
        _, settings_path = self.server.database_for(username)
        try: self._json(HTTPStatus.OK, save_settings(settings_path, value))
        except ValueError as exc: self._error(HTTPStatus.BAD_REQUEST, str(exc))


def serve(host: str = "0.0.0.0", port: int = 8787, **kwargs: Any) -> None:
    server = CloudServer((host, port), **kwargs)
    print(f"HelloFish cloud service listening on http://{host}:{port}", flush=True)
    try: server.serve_forever()
    finally: server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="HelloFish cloud service")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--username", default=os.environ.get("HELLOFISH_CLOUD_USERNAME", "admin"))
    parser.add_argument("--password", default=os.environ.get("HELLOFISH_CLOUD_PASSWORD"), help="管理员密码（也可用 HELLOFISH_CLOUD_PASSWORD）；省略后可在 Web 首次访问时设置")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()
    initial_users = {args.username: args.password} if args.password else None
    serve(args.host, args.port, data_root=args.data_root, initial_users=initial_users)
