import argparse
import csv
import html
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
from datetime import date, datetime, timedelta, timezone
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
CHINA_TZ = timezone(timedelta(hours=8))
EXPORT_COLUMNS = {
    "scanned_at": "扫描时间",
    "scan_date": "扫描日期",
    "room_name": "厅名称",
    "room_id": "厅 ID",
    "rank": "厅内排名",
    "contribution_gap": "距前一名",
    "estimated_contribution_value": "推测贡献值",
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
    "contribution_gap",
    "estimated_contribution_value",
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


def _china_today() -> date:
    """Return the scan day used by the agent, regardless of host time zone."""
    return datetime.now(timezone.utc).astimezone(CHINA_TZ).date()


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
        local_today = today or _china_today()
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
                c.charm_level, c.contribution_gap, c.estimated_contribution_value,
                c.scanned_at, c.scan_date,
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


def _format_yuan_amount(value: Any) -> str:
    amount = float(value)
    if amount < 10_000:
        display = f"{amount:.2f}".rstrip("0").rstrip(".")
        return f"{display}元"
    display = f"{amount / 10_000:.2f}".rstrip("0").rstrip(".")
    return f"{display}万元"


def _export_value(record: sqlite3.Row, column: str) -> Any:
    if column == "wealth_min_yuan":
        contribution = record["wealth_min_contribution"]
        if contribution is None:
            return "???"
        return _format_yuan_amount(contribution / 10)
    value = record[column]
    if value is None and column in {
        "wealth_level",
        "wealth_min_contribution",
        "contribution_gap",
        "estimated_contribution_value",
        "charm_level",
    }:
        return "???"
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _query_export_records(
    database_path: Path,
    settings_path: Path,
    record_query: RecordQuery,
) -> list[sqlite3.Row]:
    where_sql, parameters, order_by = _build_record_filter(settings_path, record_query)
    with closing(sqlite3.connect(database_path, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        rows = connection.execute(
            f"""
            SELECT
                c.room_id, c.room_name, c.rank, c.user_id, c.username,
                c.gender, c.ip, c.close_friend_count, c.wealth_level,
                c.charm_level, c.contribution_gap, c.estimated_contribution_value,
                c.scanned_at, c.scan_date,
                thresholds.min_contribution AS wealth_min_contribution
            FROM contributions AS c
            LEFT JOIN wealth_level_thresholds AS thresholds
                ON thresholds.level = c.wealth_level
            WHERE {where_sql}
            ORDER BY {order_by}
            """,
            parameters,
        ).fetchall()
    return rows


def export_records_csv(
    database_path: Path,
    settings_path: Path,
    record_query: RecordQuery,
    columns: list[str],
) -> bytes:
    rows = _query_export_records(database_path, settings_path, record_query)

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(EXPORT_COLUMNS[column] for column in columns)
    for record in rows:
        writer.writerow(_export_value(record, column) for column in columns)
    return b"\xef\xbb\xbf" + output.getvalue().encode("utf-8")


STATIC_EXPORT_CSS = """
:root { font-family: Inter, "Microsoft YaHei", "PingFang SC", system-ui, sans-serif; color: #17343b; background: #f4f8f7; }
* { box-sizing: border-box; }
body { margin: 0; min-width: 320px; }
.hero { padding: 42px clamp(20px, 5vw, 72px) 34px; color: white; background: linear-gradient(120deg, #123b43 0%, #176c70 62%, #238f86 100%); }
.eyebrow { margin: 0 0 8px; color: #8de6d5; font-size: 12px; font-weight: 800; letter-spacing: .18em; }
h1 { margin: 0; font-size: 46px; line-height: 1; letter-spacing: 0; }
.subtitle { margin: 14px 0 0; color: #d5efeb; }
main { padding: 26px clamp(14px, 4vw, 56px) 48px; }
.summary { display: flex; align-items: center; flex-wrap: wrap; gap: 24px; padding: 0 4px 18px; }
.summary div { display: grid; min-width: 130px; }
.summary strong { font-size: 20px; }
.summary span { color: #71888c; font-size: 11px; font-weight: 700; text-transform: uppercase; }
.summary time { margin-left: auto; color: #71888c; font-size: 12px; }
.panel { overflow: hidden; border: 1px solid #dce8e5; border-radius: 8px; background: white; box-shadow: 0 12px 38px rgba(17, 72, 76, .08); }
.filters { display: flex; flex-wrap: wrap; align-items: end; gap: 14px; padding: 18px; margin-bottom: 18px; }
.field { display: grid; gap: 7px; min-width: 130px; }
.field.wide { flex: 1 1 180px; }
.field label, .check-field { color: #536d72; font-size: 12px; font-weight: 700; }
input, select { width: 100%; border: 1px solid #cddedb; border-radius: 8px; padding: 9px 10px; color: #17343b; background: #fbfdfd; font: inherit; }
.check-field { display: flex; align-items: center; gap: 7px; padding-bottom: 8px; }
.check-field input { width: 16px; height: 16px; accent-color: #16877f; }
.filter-actions { display: flex; gap: 8px; }
.primary, .secondary { border: 0; border-radius: 8px; padding: 10px 15px; font-weight: 800; cursor: pointer; font: inherit; }
.primary { color: white; background: #177f79; }
.secondary { color: #175b5c; background: #e7f2ef; }
.table-scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; white-space: nowrap; }
th { padding: 13px 14px; color: #60797d; background: #eef5f3; font-size: 11px; text-align: left; letter-spacing: .04em; }
td { padding: 13px 14px; border-top: 1px solid #e7efed; font-size: 13px; }
tbody tr:nth-child(even) { background: #fbfdfd; }
.identity { display: grid; gap: 2px; font-weight: 700; }
.identity small { color: #819497; font-size: 10px; font-weight: 500; }
.user-copy { width: 100%; border: 0; padding: 0; color: inherit; background: none; font: inherit; text-align: left; cursor: pointer; }
.user-copy:hover { color: #176c70; }
.user-copy.copied-user, .user-copy.copied-user:hover { color: #9aa9ac; }
.user-copy.copied-user small { color: #aab7b9; }
.user-copy:focus-visible { border-radius: 4px; outline: 2px solid #238f86; outline-offset: 3px; }
.wealth { color: #b27514; font-weight: 900; }
.wealth small { margin-left: 3px; color: #8f7a54; font-size: 11px; font-weight: 600; }
.empty { padding: 58px 20px; color: #829699; text-align: center; }
.hidden { display: none !important; }
.footnote { color: #71888c; font-size: 12px; text-align: right; }
.toast { position: fixed; right: 20px; bottom: 20px; z-index: 10; max-width: calc(100vw - 40px); padding: 11px 14px; color: white; background: #17343b; border-radius: 6px; box-shadow: 0 8px 24px rgba(17, 72, 76, .22); opacity: 0; transform: translateY(8px); pointer-events: none; transition: opacity .16s ease, transform .16s ease; }
.toast.visible { opacity: 1; transform: translateY(0); }
.clipboard-fallback { position: fixed; left: -9999px; }
@media (max-width: 720px) { h1 { font-size: 36px; } .summary time { width: 100%; margin-left: 0; } }
@media print { :root { background: white; } .hero { print-color-adjust: exact; } main { padding: 18px 0; } .panel { border-radius: 0; box-shadow: none; } }
""".strip()


STATIC_EXPORT_JS = """
const toast = document.querySelector("#toast");
const copiedUserStorageKey = "hellofish-copied-user-ids-v1";
const copiedUserTtlMs = 12 * 60 * 60 * 1000;
const copiedUsers = new Map();
let toastTimer;

function restoreCopiedUsers() {
    try {
        const saved = JSON.parse(localStorage.getItem(copiedUserStorageKey) || "{}");
        const now = Date.now();
        if (saved && typeof saved === "object" && !Array.isArray(saved)) {
            for (const [userId, copiedAt] of Object.entries(saved)) {
                if (Number.isFinite(copiedAt) && now - copiedAt < copiedUserTtlMs) {
                    copiedUsers.set(userId, copiedAt);
                }
            }
        }
        localStorage.setItem(copiedUserStorageKey, JSON.stringify(Object.fromEntries(copiedUsers)));
    } catch (_) {
        // Some browsers disable storage for file:// exports; copying still works.
    }
}

function wasUserCopied(userId) {
    const copiedAt = copiedUsers.get(userId);
    return Number.isFinite(copiedAt) && Date.now() - copiedAt < copiedUserTtlMs;
}

function rememberCopiedUser(userId) {
    copiedUsers.set(userId, Date.now());
    try {
        localStorage.setItem(copiedUserStorageKey, JSON.stringify(Object.fromEntries(copiedUsers)));
    } catch (_) {
        // File exports can be opened with storage disabled.
    }
}

function showToast(message) {
    toast.textContent = message;
    toast.classList.add("visible");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove("visible"), 1800);
}

async function copyUserId(button) {
    const userId = button.dataset.userId;
    try {
        if (navigator.clipboard && window.isSecureContext) {
            await navigator.clipboard.writeText(userId);
        } else {
            const textarea = document.createElement("textarea");
            textarea.value = userId;
            textarea.setAttribute("readonly", "");
            textarea.className = "clipboard-fallback";
            document.body.append(textarea);
            textarea.select();
            const copied = document.execCommand("copy");
            textarea.remove();
            if (!copied) throw new Error("copy failed");
        }
        rememberCopiedUser(userId);
        button.classList.add("copied-user");
        showToast(`已复制用户 ID：${userId}`);
    } catch (_) {
        showToast("复制失败，请手动选择用户 ID");
    }
}

const filterForm = document.querySelector("#static-filters");
const rows = Array.from(document.querySelectorAll("#records-body tr"));
const total = document.querySelector("#record-total");
const empty = document.querySelector("#empty-state");

function fieldValue(name) {
    return filterForm.elements[name].value.trim();
}

function rowMatches(row) {
    const startDate = fieldValue("start_date");
    const endDate = fieldValue("end_date");
    const scanDate = row.dataset.scanDate;
    if ((startDate && scanDate < startDate) || (endDate && scanDate > endDate)) return false;

    const gender = fieldValue("gender");
    if (gender !== "all" && row.dataset.gender !== gender) return false;

    const wealth = row.dataset.wealth === "" ? null : Number(row.dataset.wealth);
    if (wealth === null && !filterForm.elements.include_unknown.checked) return false;
    const minimumWealth = fieldValue("min_wealth");
    if (minimumWealth && wealth !== null && wealth <= Number(minimumWealth)) return false;

    const minimumFriends = fieldValue("min_friends");
    if (minimumFriends && (row.dataset.friends === "" || Number(row.dataset.friends) < Number(minimumFriends))) return false;
    if (fieldValue("room_id") && row.dataset.roomId !== fieldValue("room_id")) return false;
    if (fieldValue("user_id") && row.dataset.userId !== fieldValue("user_id")) return false;
    const username = fieldValue("username").toLocaleLowerCase();
    return !username || row.dataset.username.toLocaleLowerCase().includes(username);
}

function applyFilters() {
    let shown = 0;
    for (const row of rows) {
        const matches = rowMatches(row);
        row.classList.toggle("hidden", !matches);
        if (matches) shown += 1;
    }
    total.textContent = shown;
    empty.classList.toggle("hidden", shown !== 0);
}

filterForm.addEventListener("submit", (event) => {
    event.preventDefault();
    applyFilters();
});
filterForm.addEventListener("reset", () => setTimeout(applyFilters));
filterForm.addEventListener("input", applyFilters);
filterForm.addEventListener("change", applyFilters);

restoreCopiedUsers();
document.querySelectorAll("[data-user-id]").forEach((button) => {
    button.classList.toggle("copied-user", wasUserCopied(button.dataset.userId));
});

document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-user-id]");
    if (button) copyUserId(button);
});
""".strip()


def _html_text(value: Any, fallback: str = "—") -> str:
    if value is None or value == "":
        return fallback
    return html.escape(str(value), quote=True)


def _format_html_scan_time(value: Any) -> str:
    if value is None or value == "":
        return "—"
    text = str(value)
    if len(text) >= 19 and text[4:5] == "-" and text[10:11] in {"T", " "}:
        text = text[:19].replace("T", " ")
    return html.escape(text, quote=True)


def _format_html_yuan(contribution: Any) -> str:
    if contribution is None:
        return "???"
    return html.escape(_format_yuan_amount(float(contribution) / 10).replace("元", " 元"))


def _static_filter_gender(value: Any) -> str:
    gender = str(value or "").strip()
    if gender == "男":
        return "male"
    if gender == "女":
        return "female"
    return "unknown"


def export_records_html(
    database_path: Path,
    settings_path: Path,
    record_query: RecordQuery,
) -> bytes:
    rows = _query_export_records(database_path, settings_path, record_query)
    table_rows: list[str] = []
    for record in rows:
        filter_values = {
            "scan-date": str(record["scan_date"] or str(record["scanned_at"] or "")[:10]),
            "wealth": "" if record["wealth_level"] is None else str(record["wealth_level"]),
            "gender": _static_filter_gender(record["gender"]),
            "friends": "" if record["close_friend_count"] is None else str(record["close_friend_count"]),
            "room-id": str(record["room_id"] or ""),
            "user-id": str(record["user_id"] or ""),
            "username": str(record["username"] or ""),
        }
        filter_attributes = " ".join(
            f'data-{name}="{html.escape(value, quote=True)}"'
            for name, value in filter_values.items()
        )
        room = (
            f'<div class="identity">{_html_text(record["room_name"])}'
            f'<small>ID {_html_text(record["room_id"])}</small></div>'
        )
        user_id = _html_text(record["user_id"], "")
        user = (
            f'<button type="button" class="identity user-copy" data-user-id="{user_id}" '
            f'title="点击复制用户 ID" aria-label="点击复制用户 ID {user_id}">'
            f'{_html_text(record["username"])}<small>ID {user_id}</small></button>'
        )
        cells = (
            _format_html_scan_time(record["scanned_at"]),
            room,
            _html_text(record["rank"]),
            _format_html_yuan(record["contribution_gap"]),
            _format_html_yuan(record["estimated_contribution_value"]),
            user,
            _html_text(record["gender"]),
            _html_text(record["ip"]),
            _html_text(record["close_friend_count"]),
            (
                f'{_html_text(record["wealth_level"], "???")}'
                f'<small>（{_format_html_yuan(record["wealth_min_contribution"])}）</small>'
            ),
            _html_text(record["charm_level"]),
        )
        table_rows.append(
            f"<tr {filter_attributes}>"
            + "".join(
                f'<td class="wealth">{value}</td>' if index == 9 else f"<td>{value}</td>"
                for index, value in enumerate(cells)
            )
            + "</tr>"
        )

    date_summary = (
        record_query.start_date.isoformat()
        if record_query.start_date == record_query.end_date
        else f"{record_query.start_date.isoformat()} — {record_query.end_date.isoformat()}"
    )
    exported_at = datetime.now(timezone.utc).astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S")
    table_content = "".join(table_rows)
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'" />
<title>HelloFish 贡献记录 {date_summary}</title>
<style>{STATIC_EXPORT_CSS}</style>
</head>
<body>
<header class="hero">
<p class="eyebrow">HELLOFISH DATA EXPORT</p>
<h1>贡献记录</h1>
<p class="subtitle">静态导出 · 金额按元显示 · 全部匹配记录</p>
</header>
<main>
<section class="summary">
<div><strong id="record-total">{len(rows)}</strong><span>条记录</span></div>
<div><strong>{date_summary}</strong><span>数据范围</span></div>
<time>导出于 {exported_at}</time>
</section>
<form id="static-filters" class="panel filters" aria-label="本地筛选条件">
<div class="field"><label for="filter-start-date">开始日期</label><input id="filter-start-date" name="start_date" type="date" /></div>
<div class="field"><label for="filter-end-date">结束日期</label><input id="filter-end-date" name="end_date" type="date" /></div>
<div class="field"><label for="filter-min-wealth">财富等级大于</label><input id="filter-min-wealth" name="min_wealth" type="number" min="0" max="300" placeholder="不限" /></div>
<label class="check-field"><input name="include_unknown" type="checkbox" checked />显示财富等级为 ??? 的记录</label>
<div class="field"><label for="filter-gender">性别</label><select id="filter-gender" name="gender"><option value="all">不限</option><option value="male">男</option><option value="female">女</option><option value="unknown">未知</option></select></div>
<div class="field"><label for="filter-min-friends">挚友数量至少</label><input id="filter-min-friends" name="min_friends" type="number" min="0" placeholder="不限" /></div>
<div class="field"><label for="filter-room-id">厅 ID</label><input id="filter-room-id" name="room_id" type="search" placeholder="精确匹配" /></div>
<div class="field"><label for="filter-user-id">用户 ID</label><input id="filter-user-id" name="user_id" type="search" placeholder="精确匹配" /></div>
<div class="field wide"><label for="filter-username">用户名</label><input id="filter-username" name="username" type="search" placeholder="包含匹配" /></div>
<div class="filter-actions"><button class="primary" type="submit">筛选</button><button class="secondary" type="reset">重置</button></div>
</form>
<section class="panel">
<div class="table-scroll">
<table>
<thead><tr><th>日期 / 时间</th><th>厅</th><th>排名</th><th>距前一名金额</th><th>推测金额</th><th>用户</th><th>性别</th><th>IP 属地</th><th>挚友</th><th>财富等级</th><th>魅力等级</th></tr></thead>
<tbody>{table_content}</tbody>
</table>
<div id="empty-state" class="empty{' hidden' if table_rows else ''}">当前条件下没有贡献记录</div>
</div>
</section>
<p class="footnote">可在本地筛选导出文件中包含的记录；金额按 10 贡献值 = 1 元换算。推测值以本次扫描最后一名为 1；缺失处按后续已知差值平均补算，第 1–3 名取相同值。</p>
</main>
<div id="toast" class="toast" role="status" aria-live="polite"></div>
<script>{STATIC_EXPORT_JS}</script>
</body>
</html>
"""
    return document.encode("utf-8")


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
        content_security_policy: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            content_security_policy
            or (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
            ),
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
            if parsed.path == "/api/export.html":
                raw_query = parse_qs(parsed.query, keep_blank_values=True)
                query = RecordQuery.from_query(raw_query)
                payload = export_records_html(
                    self.server.database_path,
                    self.server.settings_path,
                    query,
                )
                filename = f"HelloFish-contributions-{query.start_date}-{query.end_date}.html"
                self._send_bytes(
                    HTTPStatus.OK,
                    payload,
                    "text/html; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'},
                    content_security_policy=(
                        "default-src 'none'; style-src 'unsafe-inline'; "
                        "script-src 'unsafe-inline'; img-src data:; "
                        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
                    ),
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
