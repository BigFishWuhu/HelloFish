import argparse
import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen

from voice_hall_storage import VoiceHallDatabase


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "voice_hall.sqlite3"
DEFAULT_SETTINGS = PROJECT_ROOT / "data" / "contribution_viewer_settings.json"
DEFAULT_WEB_ROOT = PROJECT_ROOT / "web" / "contributions"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_URL = f"http://{DEFAULT_HOST}:{DEFAULT_PORT}/"
DEFAULT_HEALTH_URL = f"{DEFAULT_URL}api/health"
DEFAULT_LOG = PROJECT_ROOT / "data" / "contribution_viewer.log"
HEALTH_RESPONSE = {"service": "HelloFishContributionViewer"}
MAX_PAGE_SIZE = 200
EXPORT_COLUMNS = {
    "scanned_at": "扫描时间",
    "scan_date": "扫描日期",
    "room_name": "厅名称",
    "room_id": "厅 ID",
    "rank": "厅内排名",
    "username": "用户名",
    "user_id": "用户 ID",
    "gender": "性别",
    "ip": "IP 属地",
    "close_friend_count": "挚友数量",
    "wealth_level": "财富等级",
    "wealth_min_contribution": "等级最低贡献值",
    "wealth_min_yuan": "等级最低金额（元）",
    "charm_level": "魅力等级",
}
DEFAULT_EXPORT_COLUMNS = (
    "scanned_at",
    "room_name",
    "room_id",
    "rank",
    "username",
    "user_id",
    "gender",
    "ip",
    "close_friend_count",
    "wealth_level",
    "wealth_min_yuan",
    "charm_level",
)

_SETTINGS_LOCK = threading.Lock()
_SERVER_LOCK = threading.Lock()
_SERVER: "ContributionViewerServer | None" = None
_SERVER_THREAD: threading.Thread | None = None


def _normalize_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted({str(item).strip() for item in value if str(item).strip()})


def load_settings(path: Path) -> dict[str, list[str]]:
    try:
        with path.open("r", encoding="utf-8") as file:
            raw = json.load(file)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    return {
        "hidden_room_ids": _normalize_ids(raw.get("hidden_room_ids")),
        "hidden_user_ids": _normalize_ids(raw.get("hidden_user_ids")),
    }


def save_settings(path: Path, value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        raise ValueError("设置必须是 JSON 对象")
    normalized = {
        "hidden_room_ids": _normalize_ids(value.get("hidden_room_ids")),
        "hidden_user_ids": _normalize_ids(value.get("hidden_user_ids")),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with _SETTINGS_LOCK:
        with temp_path.open("w", encoding="utf-8") as file:
            json.dump(normalized, file, ensure_ascii=False, indent=2)
        temp_path.replace(path)
    return normalized


def _single(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name)
    return values[0].strip() if values else default


def _parse_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _parse_iso_date(value: str, fallback: date) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return fallback


def _date_range(query: dict[str, list[str]], today: date) -> tuple[date, date]:
    mode = _single(query, "date_mode", "today")
    if mode == "recent":
        days = _parse_int(_single(query, "days", "7"), 7, 1, 3650)
        return today - timedelta(days=days - 1), today
    if mode == "custom":
        start = _parse_iso_date(_single(query, "start_date"), today)
        end = _parse_iso_date(_single(query, "end_date"), today)
        return (end, start) if start > end else (start, end)
    return today, today


@dataclass(frozen=True)
class RecordQuery:
    start_date: date
    end_date: date
    min_wealth_level: int | None
    include_unknown: bool
    gender: str
    min_close_friend_count: int | None
    room_id: str
    user_id: str
    username: str
    page: int
    page_size: int
    sort: str

    @classmethod
    def from_query(cls, query: dict[str, list[str]], today: date | None = None) -> "RecordQuery":
        local_today = today or date.today()
        start_date, end_date = _date_range(query, local_today)
        raw_minimum = _single(query, "min_wealth_level")
        minimum = None if raw_minimum == "" else _parse_int(raw_minimum, 0, 0, 300)
        raw_minimum_friends = _single(query, "min_close_friend_count")
        minimum_friends = (
            None
            if raw_minimum_friends == ""
            else _parse_int(raw_minimum_friends, 0, 0, 1_000_000)
        )
        gender = _single(query, "gender", "all")
        if gender not in {"all", "male", "female", "unknown"}:
            gender = "all"
        return cls(
            start_date=start_date,
            end_date=end_date,
            min_wealth_level=minimum,
            include_unknown=_single(query, "include_unknown", "true").lower() == "true",
            gender=gender,
            min_close_friend_count=minimum_friends,
            room_id=_single(query, "room_id")[:100],
            user_id=_single(query, "user_id")[:100],
            username=_single(query, "username")[:100],
            page=_parse_int(_single(query, "page", "1"), 1, 1, 1_000_000),
            page_size=_parse_int(
                _single(query, "page_size", "50"), 50, 10, MAX_PAGE_SIZE
            ),
            sort=_single(query, "sort", "latest"),
        )


def _build_record_filter(
    settings_path: Path,
    record_query: RecordQuery,
) -> tuple[str, list[Any], str]:
    hidden = load_settings(settings_path)
    clauses = ["c.scan_date BETWEEN ? AND ?"]
    parameters: list[Any] = [
        record_query.start_date.isoformat(),
        record_query.end_date.isoformat(),
    ]

    if record_query.min_wealth_level is not None:
        if record_query.include_unknown:
            clauses.append("(c.wealth_level > ? OR c.wealth_level IS NULL)")
        else:
            clauses.append("c.wealth_level > ?")
        parameters.append(record_query.min_wealth_level)
    elif not record_query.include_unknown:
        clauses.append("c.wealth_level IS NOT NULL")

    if record_query.gender == "male":
        clauses.append("c.gender = '男'")
    elif record_query.gender == "female":
        clauses.append("c.gender = '女'")
    elif record_query.gender == "unknown":
        clauses.append(
            "(c.gender IS NULL OR TRIM(c.gender) = '' OR c.gender NOT IN ('男', '女'))"
        )

    if record_query.min_close_friend_count is not None:
        clauses.append("c.close_friend_count >= ?")
        parameters.append(record_query.min_close_friend_count)

    if record_query.room_id:
        clauses.append("c.room_id = ?")
        parameters.append(record_query.room_id)
    if record_query.user_id:
        clauses.append("c.user_id = ?")
        parameters.append(record_query.user_id)

    room_ids = hidden["hidden_room_ids"]
    if room_ids:
        clauses.append(f"c.room_id NOT IN ({', '.join('?' for _ in room_ids)})")
        parameters.extend(room_ids)
    user_ids = hidden["hidden_user_ids"]
    if user_ids:
        clauses.append(f"c.user_id NOT IN ({', '.join('?' for _ in user_ids)})")
        parameters.extend(user_ids)

    if record_query.username:
        escaped = (
            record_query.username.replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )
        clauses.append("c.username LIKE ? ESCAPE '\\'")
        parameters.append(f"%{escaped}%")

    order_by = {
        "wealth": "c.wealth_level DESC, c.scanned_at DESC, c.room_id, c.rank",
        "rank": "c.room_id, c.rank, c.scanned_at DESC",
        "latest": "c.scanned_at DESC, c.room_id, c.rank",
    }.get(record_query.sort, "c.scanned_at DESC, c.room_id, c.rank")
    return " AND ".join(clauses), parameters, order_by


def query_records(
    database_path: Path,
    settings_path: Path,
    record_query: RecordQuery,
) -> dict[str, Any]:
    where_sql, parameters, order_by = _build_record_filter(settings_path, record_query)

    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        total = int(
            connection.execute(
                f"SELECT COUNT(*) FROM contributions AS c WHERE {where_sql}", parameters
            ).fetchone()[0]
        )
        offset = (record_query.page - 1) * record_query.page_size
        rows = connection.execute(
            f"""
            SELECT
                c.room_id, c.room_name, c.rank, c.user_id, c.username,
                c.gender, c.ip, c.close_friend_count, c.wealth_level,
                c.charm_level, c.scanned_at, c.scan_date,
                thresholds.min_contribution AS wealth_min_contribution
            FROM contributions AS c
            LEFT JOIN wealth_level_thresholds AS thresholds
                ON thresholds.level = c.wealth_level
            WHERE {where_sql}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            (*parameters, record_query.page_size, offset),
        ).fetchall()

    return {
        "records": [dict(row) for row in rows],
        "total": total,
        "page": record_query.page,
        "page_size": record_query.page_size,
        "start_date": record_query.start_date.isoformat(),
        "end_date": record_query.end_date.isoformat(),
    }


def parse_export_columns(query: dict[str, list[str]]) -> list[str]:
    raw_columns = _single(query, "columns")
    if not raw_columns:
        return list(DEFAULT_EXPORT_COLUMNS)
    columns = []
    for column in raw_columns.split(","):
        if column in EXPORT_COLUMNS and column not in columns:
            columns.append(column)
    if not columns:
        raise ValueError("至少选择一个有效的导出列")
    return columns


def _export_value(record: sqlite3.Row, column: str) -> Any:
    if column == "wealth_min_yuan":
        contribution = record["wealth_min_contribution"]
        if contribution is None:
            return "???"
        yuan = contribution / 10
        return int(yuan) if yuan.is_integer() else yuan
    value = record[column]
    if value is None and column in {
        "wealth_level",
        "wealth_min_contribution",
        "charm_level",
    }:
        return "???"
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def export_records_csv(
    database_path: Path,
    settings_path: Path,
    record_query: RecordQuery,
    columns: list[str],
) -> bytes:
    where_sql, parameters, order_by = _build_record_filter(settings_path, record_query)
    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        rows = connection.execute(
            f"""
            SELECT
                c.room_id, c.room_name, c.rank, c.user_id, c.username,
                c.gender, c.ip, c.close_friend_count, c.wealth_level,
                c.charm_level, c.scanned_at, c.scan_date,
                thresholds.min_contribution AS wealth_min_contribution
            FROM contributions AS c
            LEFT JOIN wealth_level_thresholds AS thresholds
                ON thresholds.level = c.wealth_level
            WHERE {where_sql}
            ORDER BY {order_by}
            """,
            parameters,
        ).fetchall()

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(EXPORT_COLUMNS[column] for column in columns)
    for record in rows:
        writer.writerow(_export_value(record, column) for column in columns)
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


class ContributionViewerServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        database_path: Path,
        settings_path: Path,
        web_root: Path,
    ) -> None:
        self.database_path = database_path
        self.settings_path = settings_path
        self.web_root = web_root
        VoiceHallDatabase(database_path).initialize()
        super().__init__(server_address, ContributionViewerHandler)


class ContributionViewerHandler(BaseHTTPRequestHandler):
    server: ContributionViewerServer

    def log_message(self, format: str, *args: Any) -> None:
        print("[ContributionViewer] " + format % args)

    def _send_bytes(
        self,
        status: HTTPStatus,
        payload: bytes,
        content_type: str,
        *,
        cache: bool = False,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "public, max-age=300" if cache else "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(payload)

    def _send_json(self, status: HTTPStatus, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, payload, "application/json; charset=utf-8")

    def _send_error_json(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message})

    def _valid_host(self) -> bool:
        host = self.headers.get("Host", "").split(":", 1)[0].strip("[]").lower()
        return host in {"127.0.0.1", "localhost", "::1"}

    def do_GET(self) -> None:  # noqa: N802
        if not self._valid_host():
            self._send_error_json(HTTPStatus.FORBIDDEN, "仅允许本机访问")
            return
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self._send_json(HTTPStatus.OK, HEALTH_RESPONSE)
                return
            if parsed.path == "/api/settings":
                self._send_json(HTTPStatus.OK, load_settings(self.server.settings_path))
                return
            if parsed.path == "/api/records":
                raw_query = parse_qs(parsed.query, keep_blank_values=True)
                query = RecordQuery.from_query(raw_query)
                self._send_json(
                    HTTPStatus.OK,
                    query_records(
                        self.server.database_path,
                        self.server.settings_path,
                        query,
                    ),
                )
                return
            if parsed.path == "/api/export":
                raw_query = parse_qs(parsed.query, keep_blank_values=True)
                query = RecordQuery.from_query(raw_query)
                columns = parse_export_columns(raw_query)
                payload = export_records_csv(
                    self.server.database_path,
                    self.server.settings_path,
                    query,
                    columns,
                )
                filename = f"HelloFish-contributions-{query.start_date}-{query.end_date}.csv"
                self._send_bytes(
                    HTTPStatus.OK,
                    payload,
                    "text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                )
                return
            static_files = {
                "/": ("index.html", "text/html; charset=utf-8"),
                "/index.html": ("index.html", "text/html; charset=utf-8"),
                "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            }
            static_item = static_files.get(parsed.path)
            if static_item is None:
                self._send_error_json(HTTPStatus.NOT_FOUND, "页面不存在")
                return
            filename, content_type = static_item
            self._send_bytes(
                HTTPStatus.OK,
                (self.server.web_root / filename).read_bytes(),
                content_type,
                cache=filename != "index.html",
            )
        except (OSError, sqlite3.Error, ValueError) as exc:
            self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_PUT(self) -> None:  # noqa: N802
        if not self._valid_host():
            self._send_error_json(HTTPStatus.FORBIDDEN, "仅允许本机访问")
            return
        if urlparse(self.path).path != "/api/settings":
            self._send_error_json(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        if self.headers.get_content_type() != "application/json":
            self._send_error_json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "需要 JSON 请求")
            return
        length = _parse_int(self.headers.get("Content-Length", "0"), 0, 0, 64 * 1024)
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            self._send_json(
                HTTPStatus.OK,
                save_settings(self.server.settings_path, value),
            )
        except (json.JSONDecodeError, UnicodeDecodeError, OSError, ValueError) as exc:
            self._send_error_json(HTTPStatus.BAD_REQUEST, str(exc))


def ensure_server(
    database_path: Path = DEFAULT_DATABASE,
    settings_path: Path = DEFAULT_SETTINGS,
    web_root: Path = DEFAULT_WEB_ROOT,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> ContributionViewerServer:
    global _SERVER, _SERVER_THREAD
    with _SERVER_LOCK:
        if _SERVER is not None and _SERVER_THREAD is not None and _SERVER_THREAD.is_alive():
            return _SERVER
        if not web_root.joinpath("index.html").is_file():
            raise FileNotFoundError(f"未找到贡献记录网页：{web_root}")
        server = ContributionViewerServer(
            (host, port),
            database_path,
            settings_path,
            web_root,
        )
        thread = threading.Thread(
            target=server.serve_forever,
            name="contribution-viewer",
            daemon=True,
        )
        thread.start()
        _SERVER = server
        _SERVER_THREAD = thread
        return server


def is_server_running(
    health_url: str = DEFAULT_HEALTH_URL,
    timeout: float = 0.5,
) -> bool:
    try:
        with urlopen(health_url, timeout=timeout) as response:  # noqa: S310
            return response.status == HTTPStatus.OK and json.load(response) == HEALTH_RESPONSE
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        return False


def start_background_server(
    owner_pid: int | None = None,
    *,
    startup_timeout: float = 5.0,
    log_path: Path = DEFAULT_LOG,
) -> bool:
    """Start the viewer outside the Maa Agent process.

    The returned boolean is true when this call launched the process and false
    when a healthy viewer was already listening. The worker watches MXU's PID,
    so stopping a Maa task does not stop the viewer, while closing MXU does.
    """
    if is_server_running():
        return False

    parent_pid = owner_pid if owner_pid is not None else os.getppid()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-u",
        str(Path(__file__).resolve()),
        "--serve",
        "--owner-pid",
        str(parent_pid),
    ]
    popen_options: dict[str, Any] = {
        "cwd": str(PROJECT_ROOT),
        "env": {**os.environ, "PYTHONIOENCODING": "utf-8"},
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "close_fds": True,
    }
    if os.name == "nt":
        popen_options["creationflags"] = (
            subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        )
    else:
        popen_options["start_new_session"] = True

    with log_path.open("a", encoding="utf-8") as log_file:
        process = subprocess.Popen(command, stdout=log_file, **popen_options)

    deadline = time.monotonic() + startup_timeout
    while time.monotonic() < deadline:
        if is_server_running():
            return True
        if process.poll() is not None:
            break
        time.sleep(0.05)
    raise RuntimeError(f"贡献记录后台服务启动失败，请查看日志：{log_path}")


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        wait_for_single_object = kernel32.WaitForSingleObject
        wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait_for_single_object.restype = wintypes.DWORD
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        synchronize = 0x00100000
        wait_timeout = 0x00000102
        error_access_denied = 5
        handle = open_process(synchronize, False, pid)
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            return wait_for_single_object(handle, 0) == wait_timeout
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def serve_until_owner_exits(
    owner_pid: int | None,
    database_path: Path = DEFAULT_DATABASE,
    settings_path: Path = DEFAULT_SETTINGS,
    web_root: Path = DEFAULT_WEB_ROOT,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> None:
    if not web_root.joinpath("index.html").is_file():
        raise FileNotFoundError(f"未找到贡献记录网页：{web_root}")
    server = ContributionViewerServer(
        (host, port),
        database_path,
        settings_path,
        web_root,
    )
    print(f"[ContributionViewer] 后台服务已启动：{DEFAULT_URL}", flush=True)
    try:
        if owner_pid is None:
            server.serve_forever()
            return
        server.timeout = 1
        while _process_exists(owner_pid):
            server.handle_request()
    finally:
        server.server_close()
        print("[ContributionViewer] MXU 已退出，后台服务已停止", flush=True)


def _main() -> None:
    parser = argparse.ArgumentParser(description="HelloFish contribution viewer")
    parser.add_argument("--serve", action="store_true", help="run the HTTP service")
    parser.add_argument("--owner-pid", type=int, help="exit when this process exits")
    args = parser.parse_args()
    if not args.serve:
        parser.error("需要指定 --serve")
    serve_until_owner_exits(args.owner_pid)


if __name__ == "__main__":
    _main()
