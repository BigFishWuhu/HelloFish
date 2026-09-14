import csv
import io
import json
import sqlite3
import threading
import webbrowser
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from maa.agent.agent_server import AgentServer
from maa.context import Context
from maa.custom_action import CustomAction

from voice_hall_storage import VoiceHallDatabase


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATABASE = PROJECT_ROOT / "data" / "voice_hall.sqlite3"
DEFAULT_SETTINGS = PROJECT_ROOT / "data" / "contribution_viewer_settings.json"
DEFAULT_WEB_ROOT = PROJECT_ROOT / "web" / "contributions"
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


def _resolve_project_path(value: Any, default: Path) -> Path:
    path = Path(str(value)) if value else default
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


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
                self._send_json(HTTPStatus.OK, {"service": "HelloFishContributionViewer"})
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
) -> ContributionViewerServer:
    global _SERVER, _SERVER_THREAD
    with _SERVER_LOCK:
        if _SERVER is not None and _SERVER_THREAD is not None and _SERVER_THREAD.is_alive():
            return _SERVER
        if not web_root.joinpath("index.html").is_file():
            raise FileNotFoundError(f"未找到贡献记录网页：{web_root}")
        server = ContributionViewerServer(
            ("127.0.0.1", 0),
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


@AgentServer.custom_action("open_contribution_viewer")
class OpenContributionViewerAction(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg) -> bool:
        del context
        try:
            params = json.loads(argv.custom_action_param or "{}") or {}
            server = ensure_server(
                _resolve_project_path(params.get("database"), DEFAULT_DATABASE),
                _resolve_project_path(params.get("settings"), DEFAULT_SETTINGS),
                _resolve_project_path(params.get("web_root"), DEFAULT_WEB_ROOT),
            )
            url = f"http://127.0.0.1:{server.server_port}/"
            print(f"[ContributionViewer] 正在打开 {url}")
            return bool(webbrowser.open_new_tab(url))
        except Exception as exc:  # noqa: BLE001
            print(f"[ContributionViewer] 打开失败：{exc!r}")
            return False
